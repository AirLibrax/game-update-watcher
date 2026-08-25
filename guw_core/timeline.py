"""版本节奏时间线：根据当前日期计算 6 周大版本的阶段，生成栏位内容。

版式约定（用户定义）：
    大字 = 内容标识（版本名 / 版本号 / 下半池 / 前瞻）
    日期 = 栏位右上角
    干员 = 第三行

模型：
    一个大版本 = cycle_days 天（默认 42，鸣潮 35）
    版本制游戏两个栏位，随版本进度推进：

    第 1 周   (t < 7d)   栏位A=本版本上半池     栏位B=本版本下半池
    第 2-4 周 (7d~28d)   栏位A=下半池           栏位B=下版本前瞻(预估)
    第 5 周   (28d~35d)  栏位A=前瞻时间         栏位B=下版本时间(预估)
    第 6 周   (>=35d)    栏位A=下版本时间       栏位B=下版本上半池时间

    确定的信息直接写；推算的标「（预估）」。
    活动制游戏（方舟）：无版本号，栏位A=本版本活动，栏位B=限定寻访，栏位C=下版本预告。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from guw_core.models import GameConfig, GameUpdate

# ---------- 栏位数据结构 ----------

@dataclass
class TimelineSlot:
    label: str          # 栏位标签，如 "本版本·下半池" / "下版本·复刻"
    main: str           # 大字内容，如 "v3.5「遗音扶剑，荡梦而歌」" / "下半池" / "复刻·红丝绒"
    date: str = ""      # 日期（右上角），如 "8月1日 ~ 8月28日" / "约 8月29日 开启"
    chars: str = ""     # 干员行，如 "新干员：予愿安洁莉娜、珊比"
    estimated: bool = False  # True 则日期标黄（推算）
    source: str = "official"  # 信息来源（P1-3）：official=官方公告值 / preview=官方预告 / estimate=周期推算

    def render(self) -> str:
        parts = [self.main]
        if self.date:
            parts.append(self.date)
        if self.chars:
            parts.append(self.chars)
        return " ".join(parts)


@dataclass
class TimelineResult:
    slots: list[TimelineSlot] = field(default_factory=list)
    note: str = ""       # 底部说明，如 "依据 6 周周期推算"


def parse_date(s: str) -> date | None:
    """解析 '2026-07-10 04:00' / '2026-07-10' / '2026-07-10 04:00:00' 等。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip()[:16].strip(), fmt).date()
        except ValueError:
            continue
    return None


def fmt_date(d: date) -> str:
    return f"{d.month}月{d.day}日"


def next_version_num(num: str | None) -> str:
    """版本号 +1：4.4 → 4.5，3.9 → 3.10。无版本号返回空。"""
    if not num:
        return ""
    try:
        major, minor = num.split(".")
        return f"{major}.{int(minor) + 1}"
    except Exception:
        return ""


def _stage(version_start: date, today: date, cycle_days: int) -> int:
    """返回 1~6 周阶段。"""
    days = (today - version_start).days
    if days < 0:
        return 1
    week = days // 7 + 1
    return min(week, 6)


def _up_chars_str(update: GameUpdate) -> str:
    c = update.field_value("characters")
    if c:
        return f"新角色：{c}"
    return ""


def _half_chars_str(update: GameUpdate) -> str:
    c = update.field_value("half_characters")
    if c:
        return f"下半池角色：{c}"
    return ""


def _load_events(update: GameUpdate) -> list[dict] | None:
    """从 update.fields['events']（JSON）读事件列表。

    返回 None = 字段缺失/损坏（走 legacy 兜底）；返回 [] = 合法空窗（无任何事件）。
    """
    raw = update.field_value("events")
    if not raw:
        return None
    try:
        evs = json.loads(raw)
        return evs if isinstance(evs, list) else None
    except Exception:
        return None


def _fmt_ev(d: str) -> str:
    """事件日期 '2026-08-22' → '8月22日'（空则原样）。"""
    dd = parse_date(d)
    return fmt_date(dd) if dd else d


def build_timeline(update: GameUpdate, cfg: GameConfig, today: date | None = None) -> TimelineResult:
    """根据版本更新日 + 当前日期，生成栏位。"""
    today = today or date.today()
    start_str = update.field_value("update_time") or update.field_value("display_time") or ""
    start = parse_date(start_str)
    if not start:
        if cfg.activity_mode:
            # 事件制：空窗占位条目无开始日，仍按事件列表渲染（P1-1）
            return _activity_timeline(update, cfg, today, today)
        # 没有版本更新日：无法推算精确节奏，给宽松预估
        next_lo = today + timedelta(days=14)
        next_hi = today + timedelta(days=28)
        nv = next_version_num(update.version_num)
        slots = [
            TimelineSlot("本版本", update.display_title, source="official"),
            TimelineSlot(
                "下版本",
                f"v{nv} 约 {fmt_date(next_lo)}~{fmt_date(next_hi)}" if nv else f"约 {fmt_date(next_lo)}~{fmt_date(next_hi)}",
                estimated=True,
                source="estimate",
            ),
        ]
        return TimelineResult(slots=slots, note="未能从公告提取版本更新时间，时间为粗略预估")

    if cfg.activity_mode:
        return _activity_timeline(update, cfg, start, today)

    cycle = cfg.cycle_days
    half = cfg.half_days
    pre_ahead = cfg.preview_ahead_days

    # 下版本更新时刻：优先用官方 end_time（米哈游 end_time=版本结束=下版本更新），否则周期推算
    next_known = parse_date(update.field_value("next_update_time"))
    if next_known and next_known <= start:
        # 过期防御：下版本时间不晚于本版本开始日，多为 known_dates 未随版本推进清理，视为未知
        next_known = None
    next_start = next_known or (start + timedelta(days=cycle))
    preview_date = next_start - timedelta(days=pre_ahead)

    # 下版本已知信息（known_dates 配置或后续公告）：版本名、新角色
    next_name_known = update.field_value("next_name")
    next_chars_known = update.field_value("next_characters")
    # 过期防御：next_version 等于当前版本号说明 known_dates 未随版本推进清理，忽略
    if update.version_num and update.field_value("next_version") == update.version_num:
        next_name_known = ""
        next_chars_known = ""
    # H1 兜底：预告抽取的 next_name 不得等于当前版本名（版本切换后研发通讯等预告仍在候选池 → 自指）
    if next_name_known and next_name_known == update.version_name:
        next_name_known = ""
    # 下版本大字：有已知版本名用版本名，否则未知
    if next_name_known:
        next_title = f"v{update.field_value('next_version')}「{next_name_known}」" if update.field_value("next_version") else f"「{next_name_known}」"
    else:
        next_title = "未知版本名"

    # 下版本角色行：版本新干员 + 版本内后续寻访池（下半池）干员
    half_chars_known = update.field_value("next_half_characters")
    nb_start = parse_date(update.field_value("next_banner_start"))
    # 过期防御：后续寻访池时间不得早于本版本开始（旧版本残留）
    if nb_start and nb_start <= start:
        nb_start = None
    if half_chars_known:
        extra_half = f"下半池：{half_chars_known}"
        if nb_start:
            extra_half += f"（{fmt_date(nb_start)} 开放）"
    else:
        extra_half = ""
    next_all_chars = lambda chars_str: "；".join(
        x for x in (chars_str, extra_half) if x
    )

    # 已知的确定信息
    has_preview = bool(update.field_value("preview_time"))
    up_chars = _up_chars_str(update)
    half_chars = _half_chars_str(update)
    # 合并角色行：版本新角色 + 下半池角色（都显示，逗号分隔）
    all_chars = "、".join(x for x in (up_chars, half_chars) if x)

    stage = _stage(start, today, cycle)
    slots: list[TimelineSlot] = []

    # 官方下半池时间（卡池公告）优先，否则周期推算
    official_half = parse_date(update.field_value("half_start"))
    if official_half:
        half_date = f"{fmt_date(official_half)} 开启"
        half_est = False
    else:
        half_day = start + timedelta(days=half)
        half_date = f"{fmt_date(half_day)} 开启"
        # 已兑现的日期不再标预估（日期已过即成事实）
        half_est = half_day > today

    if has_preview and parse_date(update.field_value("preview_time")):
        p = parse_date(update.field_value("preview_time"))
        preview_text = fmt_date(p)
        preview_est = False
    elif next_known:
        # 下版本时间官方确定，但前瞻=下版本-7天只是惯例推算，需标预估
        preview_text = f"{fmt_date(preview_date)} 前后"
        preview_est = preview_date > today
    else:
        preview_text = f"{fmt_date(preview_date)} 前后"
        preview_est = preview_date > today

    if stage == 1:
        # 刚开版本：上下半池
        slots.append(TimelineSlot(
            "本版本·上半池",
            update.display_title,
            f"{fmt_date(start)} 开启",
            up_chars,
            source="official",
        ))
        slots.append(TimelineSlot(
            "本版本·下半池",
            "下半池",
            half_date,
            half_chars,
            estimated=half_est,
            source="official" if official_half else "estimate",
        ))
    elif stage <= 4:
        # 2-4 周：下半池 + 前瞻预估（角色合并显示：版本新角色+下半池）
        slots.append(TimelineSlot(
            "本版本·下半池",
            "下半池",
            half_date,
            all_chars,
            estimated=half_est,
            source="official" if official_half else "estimate",
        ))
        slots.append(TimelineSlot(
            "下版本·前瞻",
            "前瞻",
            preview_text,
            estimated=preview_est,
            source="official" if (has_preview and parse_date(update.field_value("preview_time"))) else "estimate",
        ))
    elif stage == 5:
        # 第 5 周：前瞻 + 下版本时间（本版本角色仍显示在栏位A）
        slots.append(TimelineSlot(
            "下版本·前瞻",
            "前瞻",
            preview_text,
            all_chars,
            estimated=preview_est,
            source="official" if (has_preview and parse_date(update.field_value("preview_time"))) else "estimate",
        ))
        next_chars = next_all_chars(f"新角色：{next_chars_known}" if next_chars_known else "")
        slots.append(TimelineSlot(
            "下版本·更新时间",
            next_title,
            f"{fmt_date(next_start)}" if next_known else f"约 {fmt_date(next_start)}",
            next_chars,
            # 官方值不标预估；推算值已过也不标（日期已兑现为事实）
            estimated=(not next_known) and next_start > today,
            source="official" if next_known else "estimate",
        ))
    else:
        # 第 6 周：下版本时间 + 下版本上半池（本版本角色仍显示）
        next_chars = next_all_chars(f"新角色：{next_chars_known}" if next_chars_known else "")
        slots.append(TimelineSlot(
            "下版本·更新时间",
            next_title,
            f"{fmt_date(next_start)}" if next_known else f"约 {fmt_date(next_start)}",
            next_chars,
            estimated=(not next_known) and next_start > today,
            source="official" if next_known else "estimate",
        ))
        slots.append(TimelineSlot(
            "下版本·上半池",
            "上半池",
            f"{fmt_date(next_start)} 开启" if next_known else f"约 {fmt_date(next_start)} 开启",
            estimated=(not next_known) and next_start > today,
            source="official" if next_known else "estimate",
        ))

    note = "确定信息来自官方公告，其余按 6 周周期推算"
    return TimelineResult(slots=slots, note=note)


def _activity_timeline(update: GameUpdate, cfg: GameConfig, start: date, today: date) -> TimelineResult:
    """活动制（方舟）：事件列表状态机渲染（P1-1）。

    栏位A = 主事件：进行中的 main/reprint 取 start 最新；无则最近 upcoming（"即将开启"）；
            完全无事件 → 空窗占位（"暂无活动，等待官方公告"）。
    栏位B = 并行进行中的其他活动；无则 roadmap 官方预告（粗窗口字符串如"9月上旬"，
            按原始字符串展示，不假装精确日期）；再无 → 未知。
    """
    events = _load_events(update)
    if events is None:
        # 兼容兜底：事件列表缺失时按旧字段渲染单栏（理论上 P1-1 后不会触发）
        return _activity_timeline_legacy(update, start, today)
    if not events:
        # 合法空窗：无任何事件（含 roadmap）
        return TimelineResult(
            slots=[TimelineSlot("本版本", "暂无活动", "等待官方公告", source="estimate")],
            note="暂无进行中活动，等待官方公告",
        )

    today_s = today.isoformat()
    main_evs = [ev for ev in events if ev["kind"] in ("main", "reprint")]
    actives = [ev for ev in main_evs if ev["start"] and ev["end"] and ev["start"] <= today_s <= ev["end"]]
    slots: list[TimelineSlot] = []

    if actives:
        me = max(actives, key=lambda ev: ev["start"])
        label = "本版本·复刻" if me["kind"] == "reprint" else "本版本"
        # 干员行：复刻/新干员 + 寻访池说明（banner 附属信息）
        chars = update.fields.get("characters")
        char_list = [c.strip() for c in chars.value.split(",") if c.strip()] if chars else []
        a_chars = ""
        if char_list:
            prefix = "复刻干员：" if me["kind"] == "reprint" else "新干员："
            a_chars = prefix + "、".join(char_list[:8])
            banner_name = update.field_value("banner_name")
            b_start = parse_date(update.field_value("banner_start"))
            b_end = parse_date(update.field_value("banner_end"))
            if banner_name:
                if b_start and b_end:
                    a_chars += f"（寻访 {banner_name} {fmt_date(b_start)}~{fmt_date(b_end)}）"
                elif b_end:
                    a_chars += f"（寻访 {banner_name} 至 {fmt_date(b_end)}）"
                else:
                    a_chars += f"（寻访池：{banner_name}）"
            elif b_end:
                a_chars += f"（寻访至 {fmt_date(b_end)}）"
        slots.append(TimelineSlot(
            label, f"「{me['name']}」",
            f"{_fmt_ev(me['start'])} ~ {_fmt_ev(me['end'])}",
            a_chars, source="official",
        ))
        others = [ev for ev in actives if ev is not me]
    else:
        upcomings = [ev for ev in main_evs if ev["start"] and ev["start"] > today_s]
        if upcomings:
            me = min(upcomings, key=lambda ev: ev["start"])
            slots.append(TimelineSlot(
                "下版本·即将开启", f"「{me['name']}」",
                f"{_fmt_ev(me['start'])} 开启", source="official",
            ))
        else:
            # 空窗期：无进行中、无预告
            slots.append(TimelineSlot("本版本", "暂无活动", "等待官方公告", source="estimate"))
        others = []

    # 栏位B：并行活动 > roadmap 官方预告 > 未知
    if others:
        o = others[0]
        slots.append(TimelineSlot(
            "并行活动", f"「{o['name']}」",
            f"{_fmt_ev(o['start'])} ~ {_fmt_ev(o['end'])}", source="official",
        ))
    else:
        roadmaps = [ev for ev in events if ev["kind"] == "roadmap" and ev.get("rough")]
        if roadmaps:
            r = roadmaps[0]
            slots.append(TimelineSlot("下版本·官方预告", f"「{r['name']}」", r["rough"], source="preview"))
        else:
            slots.append(TimelineSlot("下版本", "未知"))

    return TimelineResult(slots=slots, note="活动制游戏：本版本来自官方公告，下版本为官方预告/推算")


def _activity_timeline_legacy(update: GameUpdate, start: date, today: date) -> TimelineResult:
    """事件列表缺失时的兜底渲染（保留旧行为，正常流程不触发）。"""
    cycle = 28
    official_end = parse_date(update.field_value("activity_end"))
    act_end = official_end or (start + timedelta(days=cycle - 1))
    next_start = start + timedelta(days=cycle)
    chars = update.fields.get("characters")
    char_list = [c.strip() for c in chars.value.split(",") if c.strip()] if chars else []
    is_reprint = bool(update.field_value("is_reprint"))
    a_chars = ""
    if char_list:
        prefix = "复刻干员：" if is_reprint else "新干员："
        a_chars = prefix + "、".join(char_list[:8])
        banner_name = update.field_value("banner_name")
        b_end = parse_date(update.field_value("banner_end"))
        if banner_name:
            a_chars += f"（寻访池：{banner_name}）"
        elif b_end:
            a_chars += f"（寻访至 {fmt_date(b_end)}）"
    slots = [TimelineSlot(
        "本版本·复刻" if is_reprint else "本版本",
        update.display_title,
        f"{fmt_date(start)} ~ {fmt_date(act_end)}",
        a_chars, source="official",
    )]
    next_name = update.field_value("next_activity")
    if next_name:
        slots.append(TimelineSlot("下版本", f"「{next_name}」", f"约 {fmt_date(next_start)} 开启", source="estimate"))
    else:
        slots.append(TimelineSlot("下版本", "未知"))
    return TimelineResult(slots=slots, note="活动制游戏：本版本来自官方公告，下版本待官方预告")
