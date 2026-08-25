"""交叉认证引擎 + 字段解析规则。

两层：
1. extract: 从 adapter 原始条目（标题+正文）按游戏配置的规则提取目标字段声明
2. aggregate: 同字段多源声明加权聚合，计算置信度（字段级交叉认证）
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from guw_core.models import FieldClaim, FieldVerdict, GameConfig

# ---------- 通用时间/内容提取正则 ----------

DATE_CN = r"\d{4}年\d{1,2}月\d{1,2}日"
DATE_MD = r"\d{1,2}月\d{1,2}日"
TIME_HM = r"\d{1,2}:\d{2}"

# 维护/更新时间：如 "2026年7月10日04:00 ~ 2026年7月10日11:00（UTC+8）"
RE_MAINT = re.compile(
    rf"((?:维护|更新)[^：:]*[：:]\s*)?({DATE_CN}\s*{TIME_HM}?\s*[~～\-—至到]\s*{DATE_CN}\s*{TIME_HM}?)"
)
# 前瞻/直播时间：如 "前瞻通讯将于2026年8月7日19:00 播出" / "8月7日19:00"
RE_PREVIEW = re.compile(
    rf"(前瞻|特别通讯|直播|前瞻通讯)[^。\n]{{0,40}}({DATE_CN}|{DATE_MD})\s*({TIME_HM})?"
)
# 新角色：优先从「xxx」角色活动唤取/限时寻访模式提取（米哈游/库洛/鹰角公告正文常见）
#   例：5星共鸣者「秧秧・玄翎」（湮灭 | 迅刀） / 5星「姬子•启行（智识•火）」/ 6星干员【诀】
#   负向前瞻排除 光锥/武器/音擎/遗器；捕获组排除 冒号/斜杠 防止 "包括：诀/卡缪" 整串被当角色
RE_CHAR_BANNER = re.compile(
    r"(?:★{1,6}|\d\s*星|五星|四星|S级|A级)\s*(?![^「』【】]{0,6}?(?:光锥|武器|音擎|遗器))\s*(?:共鸣者|角色|干员|代理人|特勤|英雄|邦布)?\s*[「『\[【]?\s*([^」』\]「『【】\s,，、:：/（）()\n]{1,12})\s*[」』\]】]?"
)
# 兜底：正文里的「xxx」词组（排除明显不是角色的）
RE_CHARS = re.compile(r"[「『]([^」』]{1,12})[」』]")

# 下半池角色：如 "刻律德菈、那刻夏与砂金则将于下半回归跃迁" / "将于版本下半登场"
RE_HALF_CHAR = re.compile(
    r"([\u4e00-\u9fff·、]{2,24}?)(?:则)?(?:将)?于(?:版本)?下半(?:期)?(?:回归|复刻|登场|开启|跃迁|活动)"
)
BAD_CHAR_WORDS = (
    "维护", "更新", "补偿", "时间", "活动", "版本", "前瞻", "直播",
    "皮肤", "时装", "套装", "奖励", "商店", "任务", "概率",
    "说明", "公告", "开启", "上线", "兑换", "签到", "礼包", "联动",
    "优化", "修复", "问题", "调整", "追加", "亲爱的", "漂泊者", "玩家",
    "博士", "绳匠", "开拓者", "内容", "介绍", "敬请", "关注",
    "光锥", "武器", "遗器", "装饰", "头像", "名片", "宠物", "家具",
    "音擎", "邦布", "模型",
)

# 活动标题后缀清理：去掉公告类后缀词，只留活动名核心
_ACTIVITY_SUFFIXES = (
    "限定寻访开启", "限定寻访说明", "活动限时开启", "限时开启", "开启",
    "创作征集活动进行中", "创作征集", "复刻活动", "活动预告", "即将开启",
    "复刻", "说明", "公告",
)


def _clean_activity_name(title: str) -> str:
    """把 '车辙与风的归所限定寻访开启' 清理为 '车辙与风的归所'。"""
    name = title.strip()
    for suf in sorted(_ACTIVITY_SUFFIXES, key=len, reverse=True):
        if name.endswith(suf):
            name = name[: -len(suf)].strip()
            break
    return name or title.strip()


def _fmt_date_hm(s: str) -> str:
    """把 '2026年7月10日 04:00' 归一为 '2026-07-10 04:00'。"""
    s = s.strip()
    try:
        m = re.match(rf"({DATE_CN})\s*({TIME_HM})?", s)
        if not m:
            return s
        date_s, time_s = m.group(1), m.group(2)
        dt = datetime.strptime(date_s, "%Y年%m月%d日")
        return f"{dt:%Y-%m-%d}" + (f" {time_s}" if time_s else "")
    except Exception:
        return s


def _strip_html(s: str) -> str:
    """剥掉 HTML 标签，保留可读文本（各 adapter 的 content 可能是原始 HTML）。"""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</?(?:div|p|li|h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return s


def _extract_section(text: str, starts: tuple[str, ...], ends: tuple[str, ...]) -> str:
    """取 start 关键词到第一个 end 关键词之间的文本。找不到则返回全文。"""
    if not starts:
        return text
    start_pos = -1
    start_len = 0
    for s in starts:
        pos = text.find(s)
        if pos != -1 and (start_pos == -1 or pos < start_pos):
            start_pos = pos
            start_len = len(s)
    if start_pos == -1:
        return text
    end_pos = len(text)
    for e in ends:
        pos = text.find(e, start_pos + start_len)
        if pos != -1 and pos < end_pos:
            end_pos = pos
    return text[start_pos:end_pos]


def extract_fields(item: dict[str, Any], cfg: GameConfig) -> list[FieldClaim]:
    """把 adapter 原始条目按游戏配置提取为目标字段声明。"""
    claims: list[FieldClaim] = item["claims"]
    title = item.get("raw_title", "")

    # 已知确定时间覆盖（games/*.json 的 known_dates，官方已官宣）
    # weight 用 1.5：保证 aggregate 必然选 known 值，而非依赖插入顺序
    known = cfg.known_dates
    known_claims: list[FieldClaim] = []
    for fld, val in known.items():
        if val:
            known_claims.append(FieldClaim(fld, val, "known", 1.5, ""))

    # 正文合并（多段 content 拼接），统一剥 HTML 再匹配
    contents = [c.value for c in claims if c.field == "content"]
    content = "\n".join(_strip_html(c) for c in contents)
    source_url = item.get("url", "")

    out: list[FieldClaim] = []

    # 1. 版本号+版本名：优先用配置的命名分组正则从标题提取
    #    约定分组名：(?P<num>...) 版本号 / (?P<name>...) 版本名或活动名
    #    方舟（活动制）：优先从正文提取 SideStory 名（如 SideStory「直到大地变成一颗酸橙」）
    vp = cfg.version_pattern
    got_name = False
    if cfg.activity_mode and content:
        m_ss = re.search(r"SideStory「([^」]+)」|「([^」]+)」活动开启|「([^」]+)」限时活动", content)
        if m_ss:
            name = next((g for g in m_ss.groups() if g), "")
            if name:
                out.append(FieldClaim("version_name", name.strip(), "parse", 1.0, source_url))
                got_name = True
        # 活动/关卡时间："开放时间：08月01日 12:00 - 08月22日 03:59"（关卡）或
        # "活动时间：08月01日 12:00 - 08月29日 03:59"（含商店）
        # 版本结束以关卡时间为准：优先"开放时间"，没有才用"活动时间"
        # 预告类公告（"即将开启"/"活动预告"）：优先"活动时间"（预告第一段即完整活动期，如复刻 8/22~9/5），
        # 且活动时间同时映射到 next_activity_start/end（该活动属于"下版本"，供栏位B官方日期与预告升格）
        is_preview = any(k in title for k in ("即将开启", "活动预告"))
        m_at = None
        for kw in (("活动时间", "开放时间") if is_preview else ("开放时间", "活动时间")):
            m_at = re.search(
                rf"{kw}[：:]\s*(\d{{1,2}})月(\d{{1,2}})日[^\n]*?[-—~～]\s*(\d{{1,2}})月(\d{{1,2}})日",
                content,
            )
            if m_at:
                break
        if m_at:
            # 年份取当前年（公告通常年内活动）
            year = datetime.now().year
            start_s = f"{year}-{int(m_at.group(1)):02d}-{int(m_at.group(2)):02d}"
            end_s = f"{year}-{int(m_at.group(3)):02d}-{int(m_at.group(4)):02d}"
            out.append(FieldClaim("update_time", start_s, "parse", 1.0, source_url))
            out.append(FieldClaim("activity_end", end_s, "parse", 1.0, source_url))
            if is_preview:
                out.append(FieldClaim("next_activity_start", start_s, "parse", 1.0, source_url))
                out.append(FieldClaim("next_activity_end", end_s, "parse", 1.0, source_url))
        else:
            # 正文没有活动时间，用公告发布日期兜底（displayTime）
            for c in claims:
                if c.field == "display_time" and c.value:
                    out.append(FieldClaim("update_time", c.value, "parse", 1.0, source_url))
                    break
        # 限定寻访时间："二、「夏日嘉年华」，【车辙与风的归所】限定寻访开启" 后跟 "活动时间：08月01日 - 08月15日"
        # 中间可能跨行（开启 在上一行，活动时间 在下一行）
        m_banner = re.search(r"【([^】]+)】限定寻访.*?活动时间[：:]\s*(\d{1,2})月(\d{1,2})日.*?[-—~～]\s*(\d{1,2})月(\d{1,2})日", content, re.S)
        if m_banner:
            year = datetime.now().year
            out.append(FieldClaim(
                "banner_name", m_banner.group(1).strip(), "parse", 1.0, source_url,
            ))
            out.append(FieldClaim(
                "banner_start",
                f"{year}-{int(m_banner.group(2)):02d}-{int(m_banner.group(3)):02d}",
                "parse", 1.0, source_url,
            ))
            out.append(FieldClaim(
                "banner_end",
                f"{year}-{int(m_banner.group(4)):02d}-{int(m_banner.group(5)):02d}",
                "parse", 1.0, source_url,
            ))
    if vp:
        m = re.search(vp, title)
        if m:
            name = m.groupdict().get("name") or (m.group(1) if m.lastindex == 1 else "")
            num = m.groupdict().get("num")
            if name and not got_name:
                out.append(FieldClaim("version_name", name.strip(), "parse", 1.0, source_url))
            if num:
                out.append(FieldClaim("version", num.strip(), "parse", 1.0, source_url))
        elif not got_name:
            # 提不到版本号但有「」名字 → 活动制（方舟）
            nm = re.search(r"[「『]([^」』]+)[」』]", title)
            if nm:
                out.append(FieldClaim("version_name", nm.group(1).strip(), "parse", 1.0, source_url))
            elif title:
                out.append(FieldClaim("version_name", _clean_activity_name(title), "parse", 1.0, source_url))
    elif title and not got_name:
        nm = re.search(r"[「『]([^」』]+)[」』]", title)
        name = nm.group(1).strip() if nm else _clean_activity_name(title)
        out.append(FieldClaim("version_name", name, "parse", 1.0, source_url))

    # 2. 更新时间（维护时间）：鸣潮等用中文日期；米哈游用 ISO/斜杠格式，正文提取失败则透传 adapter 的 start_time
    maint_found = False
    for m in RE_MAINT.finditer(content):
        out.append(FieldClaim("update_time", _fmt_date_hm(m.group(2)), "parse", 1.0, source_url))
        maint_found = True
    if not maint_found:
        for c in claims:
            if c.field in ("update_time", "start_time") and c.value:
                out.append(FieldClaim("update_time", c.value, "parse", 1.0, source_url))
                break
    # 2b. 下版本更新时刻：米哈游的 end_time 即版本结束=下版本更新时间（如绝区零 end 2026-09-09 = 3.2 更新）
    for c in claims:
        if c.field == "end_time" and c.value:
            out.append(FieldClaim("next_update_time", c.value, "parse", 1.0, source_url))
            break

    # 3. 前瞻时间
    for m in RE_PREVIEW.finditer(content):
        out.append(FieldClaim("preview_time", _fmt_date_hm(f"{m.group(2)} {m.group(3) or ''}"), "parse", 1.0, source_url))

    # 4. 新角色：策略按游戏类型区分，宁可少抓不可抓错
    #    - 方舟（活动制）：用 ★★★★：干员名 格式提取
    #    - 库洛（鸣潮）：正文「」基本都是角色名/版本名，允许兜底
    #    - 米哈游：只用星级前缀模式（全新五星角色「xxx」），避免抓错
    char_names: list[str] = []
    if cfg.activity_mode:
        # 方舟：当期新干员 = 主活动公告"新干员登场"段的全部星级干员
        #   原文：五、「夏日嘉年华」新干员登场
        #   ★★★★★★：予愿安洁莉娜[限定]
        #   ★★★★★★：珊比
        #   ★★★★★：嘉辛塔
        #   ★★★★★：时隙
        def _extract_ark_seg(seg: str) -> list[str]:
            out: list[str] = []
            seg = re.split(r"[（(]", seg)[0]
            for part in re.split(r"[\\/、,，]", seg):
                name = part.strip()
                name = re.sub(r"\[.*?\]", "", name).strip()  # 去 [限定]
                name = re.sub(r"[【】\[\]]", "", name).strip()  # 清全角/半角括号残留
                name = re.sub(r"[\d*×%：:]\s*", "", name).strip()
                if name and not any(w in name for w in BAD_CHAR_WORDS) and 2 <= len(name) <= 12:
                    out.append(name)
            return out

        # 锚定"新干员"段：找到"新干员"或"新增干员"字样后，提取其后的全部星级行
        # 6★ 和 5★ 行混在一起匹配（同一字符类），保证两种星级都被提取
        m_new = re.search(
            r"(?:新干员登场|新增干员)[^★]{0,80}?((?:(?:★{6}|★{5})[：:][^★\n】]+\s*\n?\s*)+)",
            content,
        )
        if m_new:
            block = m_new.group(1)
            for mm in re.finditer(r"★{6}[：:]\s*([^★\n】]+)|★{5}[：:]\s*([^★\n】]+)", block):
                seg = mm.group(1) or mm.group(2)
                char_names.extend(_extract_ark_seg(seg))
        # 兜底：没有"新干员"段时，取第一个6★段 + 第一个5★段
        if not char_names:
            m6 = re.search(r"★{6}[：:]\s*([^★\n】]+)", content)
            if m6:
                char_names.extend(_extract_ark_seg(m6.group(1)))
            m5 = re.search(r"★{5}[：:]\s*([^★\n】]+)", content)
            if m5:
                char_names.extend(_extract_ark_seg(m5.group(1)))
    else:
        # 版本制游戏：锚定角色章节提取，避免武器/装备名混入
        allow_bracket_fallback = cfg.adapter == "kuro_json"
        if cfg.adapter == "mihoyo_json":
            # 米哈游：2、全新角色 → 3、全新光锥（绝区零是 全新代理人 → 全新音擎）
            sec = _extract_section(
                content,
                ("全新角色", "新角色", "全新代理人", "新代理人"),
                ("全新光锥", "全新武器", "全新音擎", "全新邦布", "全新装扮", "全新场景", "全新活动", "全新剧情"),
            )
            for m in RE_CHAR_BANNER.finditer(sec):
                name = m.group(1).strip()
                if name and not any(w in name for w in BAD_CHAR_WORDS) and not re.search(r"[\d*×%]|：|:", name):
                    char_names.append(name)
        elif cfg.adapter == "hg_ssr":
            # 终末地：■ 全新干员 → ■ 全新武器（干员行：6星干员【诀】【梨诺】）
            sec = _extract_section(content, ("全新干员", "新增干员"), ("全新武器", "全新敌人", "全新区域", "全新寻访", "全新活动", "全新剧情", "全新场景"))
            for m in RE_CHAR_BANNER.finditer(sec):
                name = m.group(1).strip()
                if name and not any(w in name for w in BAD_CHAR_WORDS) and not re.search(r"[\d*×%]|：|:", name):
                    char_names.append(name)
            # 补充："6星干员【诀】【梨诺】" 并列的【】名字（第二个起无星级前缀）
            for m in re.finditer(r"【([^】]{1,8})】", sec):
                name = m.group(1).strip()
                if not name or name in char_names:
                    continue
                if any(w in name for w in BAD_CHAR_WORDS) or re.search(r"[\d*×%]|：|:", name):
                    continue
                if 2 <= len(name) <= 8:
                    char_names.append(name)
        else:
            for m in RE_CHAR_BANNER.finditer(content):
                name = m.group(1).strip()
                if name and not any(w in name for w in BAD_CHAR_WORDS) and not re.search(r"[\d*×%]|：|:", name):
                    char_names.append(name)
        if allow_bracket_fallback and not char_names:
            for m in RE_CHARS.finditer(content):
                name = m.group(1).strip()
                if not name:
                    continue
                if any(w in name for w in BAD_CHAR_WORDS) or re.search(r"[\d*×%]|：|:", name):
                    continue
                if 2 <= len(name) <= 8:
                    char_names.append(name)
    # 去重保序
    seen_c = set()
    for name in char_names:
        if name not in seen_c:
            seen_c.add(name)
            out.append(FieldClaim("characters", name, "parse", 1.0, source_url))

    # 5. 下半池角色（如崩铁："刻律德菈、那刻夏与砂金则将于下半回归跃迁"）
    half_chars: list[str] = []
    for m in RE_HALF_CHAR.finditer(content):
        seg = m.group(1)
        names = [n.strip() for n in re.split(r"[、与和、/，,]", seg) if n.strip()]
        for n in names:
            n = re.sub(r"[\d*×%]|：|:", "", n).strip()
            if n and not any(w in n for w in BAD_CHAR_WORDS) and 2 <= len(n) <= 8:
                half_chars.append(n)
    for n in half_chars:
        out.append(FieldClaim("half_characters", n, "parse", 1.0, source_url))

    # 5b. 米哈游卡池公告："活动跃迁（其二）" / "调频（第二期）" 提供官方下半池开始时间
    #     这类公告不是版本条目，只提取 half_start 供主条目使用
    if "（其二）" in title or "（第二期）" in title or "下半" in title:
        for c in claims:
            if c.field == "start_time" and c.value:
                out.append(FieldClaim("half_start", c.value, "parse", 1.0, source_url))
                break
    # 5c. 通用卡池时间提取：正文里"跃迁时间为 2026/07/15 ... - 2026/08/25" 模式
    #     第二组"跃迁时间为"通常是下半池（如崩铁：刻律德菈等返场，跃迁时间为 2026/08/05 - 08/25）
    #     同时兼容终末地"特许寻访 · 开放时间"格式
    if not any(c.field == "half_start" for c in out):
        # 米哈游格式：第二组跃迁时间
        jump_times = list(re.finditer(r"跃迁时间为\s*(\d{4})/(\d{2})/(\d{2})", content))
        if len(jump_times) >= 2:
            m = jump_times[1]
            out.append(FieldClaim("half_start", f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "parse", 1.0, source_url))
        else:
            # 终末地格式：特许寻访开放时间（第一个具体日期即下半池开始）
            for m in re.finditer(r"特许寻访[^。]{0,120}?开放时间[：:]\s*\S*?(\d{4})/(\d{2})/(\d{2})", content):
                out.append(FieldClaim(
                    "half_start",
                    f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                    "parse", 1.0, source_url,
                ))
                break

    # 6. 链接
    if source_url:
        out.append(FieldClaim("link", source_url, "parse", 1.0, source_url))

    # 7. 已知确定时间覆盖：追加到末尾，aggregate 按权重取最高（known=1.0 与 parse 持平，但排在后面会被归一化去重）
    for c in known_claims:
        out.append(c)

    return out


def extract_preview_claims(item: dict[str, Any], cfg: GameConfig, main_start: date) -> list[FieldClaim]:
    """预告类条目 → next_* 字段声明（版本名/更新时间/新角色）。

    供 preview_sources 配置的预告池使用（如终末地「X」版本研发通讯、B站官方活动说明动态）：
    - next_name：标题「版本名」→ 下版本名（「雪凇幽梦」版本研发通讯 → 雪凇幽梦）
    - next_update_time：正文/动态文本里的斜杠日期 YYYY/MM/DD [HH:MM]，
      取落在 [主版本更新日 + cycle±10 天] 窗口内的第一个（排除当期活动日期与已过去日期）
    - next_characters：星级前缀角色（仅预告正文明确写"干员/角色"的场景，防误抓）
    """
    out: list[FieldClaim] = []
    title = item.get("raw_title", "")
    url = item.get("url", "")
    contents = [c.value for c in item.get("claims", []) if c.field == "content"]
    content = "\n".join(_strip_html(c) for c in contents)

    # next_name：标题「」版本名
    m = re.search(r"[「『]([^」』]{1,20})[」』]", title)
    if m:
        name = m.group(1).strip()
        if name and not any(w in name for w in BAD_CHAR_WORDS) and not re.search(r"[\d*×%]|：|:", name):
            out.append(FieldClaim("next_name", name, "parse", 1.0, url))

    # next_update_time：斜杠日期 + 版本周期窗口
    lo = main_start + timedelta(days=cfg.cycle_days - 10)
    hi = main_start + timedelta(days=cfg.cycle_days + 10)
    for mm in re.finditer(r"(\d{4})/(\d{1,2})/(\d{1,2})\s*(\d{1,2}:\d{2})?", content):
        try:
            d = datetime(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
        except ValueError:
            continue
        if lo <= d.date() <= hi:
            time_s = mm.group(4) or ""
            out.append(FieldClaim(
                "next_update_time",
                f"{d:%Y-%m-%d}" + (f" {time_s}" if time_s else ""),
                "parse", 1.0, url,
            ))
            break

    # next_characters：星级前缀角色
    for m in RE_CHAR_BANNER.finditer(content):
        name = m.group(1).strip()
        if name and not any(w in name for w in BAD_CHAR_WORDS) and not re.search(r"[\d*×%]|：|:", name):
            out.append(FieldClaim("next_characters", name, "parse", 1.0, url))
    return out


def aggregate(claims: list[FieldClaim], publish_threshold: float) -> dict[str, FieldVerdict]:
    """字段级交叉认证：同字段多源声明 → 加权置信度。

    置信度 = 最高源权重 + 0.15 × (一致源数 - 1)，上限 1.0。
    字段声明之间 value 归一化后相同视为「一致」。
    """
    by_field: dict[str, list[FieldClaim]] = {}
    for c in claims:
        if not c.value:
            continue
        by_field.setdefault(c.field, []).append(c)

    verdicts: dict[str, FieldVerdict] = {}
    for field, fs in by_field.items():
        if field in ("raw_title", "content", "ann_id", "category", "display_time", "tag_label",
                     "start_time", "end_time", "start_time_ms", "end_time_ms", "publish_time_ms"):
            continue
        if field in ("characters", "half_characters"):
            seen_vals: list[str] = []
            for c in fs:
                v = c.value.strip()
                if v and v not in seen_vals:
                    seen_vals.append(v)
            if seen_vals:
                verdicts[field] = FieldVerdict(
                    field=field,
                    value=",".join(seen_vals),
                    confidence=1.0,
                    sources=sorted({c.source for c in fs}),
                    pending=False,
                )
            continue
        # 按归一化值分组
        groups: dict[str, list[FieldClaim]] = {}
        for c in fs:
            groups.setdefault(c.normalized, []).append(c)

        # 取包含最高权重源的那组为主值；其余组视为候选
        best_group = max(groups.values(), key=lambda g: max(c.weight for c in g))
        best_val = best_group[0].value
        best_weight = max(c.weight for c in best_group)

        # 其他组中与 best 归一化一致的（同一组），以及跨组一致的要数进来
        # 简化：统计有多少不同源（source_id）的值归一化后与 best 一致
        consistent_sources = {c.source for c in best_group}
        for other in fs:
            if other.normalized == best_group[0].normalized:
                consistent_sources.add(other.source)
        n = len(consistent_sources)

        conf = min(1.0, best_weight + 0.15 * (n - 1))
        pending = conf < publish_threshold
        verdicts[field] = FieldVerdict(
            field=field,
            value=best_val,
            confidence=conf,
            sources=sorted(consistent_sources),
            pending=pending,
        )
    return verdicts
