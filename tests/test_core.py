# -*- coding: utf-8 -*-
"""game-update-watcher 离线合成回归（零第三方依赖，纯 assert）。

运行: python tests/test_core.py   （P0~P2 历轮修复的固化回归）
覆盖：
  R1  H1 自指守卫（版本切换后预告残留不注入 next_name）
  R2  版本制空锚点筛选（update_time 缺失条目不参与 ≤today 比较）
  R3  来源标注 source（官方/推算）与宽松预估分支标注
  R4  方舟事件状态机：active+roadmap / upcoming / 空窗 / 并行活动 / banner 寻访池 / 粗窗口
  R5  LLM 兜底护栏：合法注入(conf=0.8) / 越界拒绝 / 每轮限 1 次 / 正则命中零调用
  R6  known_dates 与自动值合并去重（伊冯上半/下半去重案例）
  R7  预告插值识别（"即将于8月22日04:00开启" → 活动时间/活动时间优先）
  R8  RE_HALF_CHAR 长名单提取（>24 字符不截断）
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from guw_core.models import FieldClaim, GameConfig  # noqa: E402
from guw_core.pipeline import UpdatePipeline  # noqa: E402
from guw_core.timeline import build_timeline  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"[PASS] {name}")
    else:
        _FAIL += 1
        print(f"[FAIL] {name} {detail}")


def item(title, content="", extra_claims=None, source="bili_dynamic", display_time=""):
    claims = [FieldClaim(field="raw_title", value=title, source=source, weight=0.9)]
    if display_time:
        claims.append(FieldClaim(field="display_time", value=display_time, source=source, weight=0.9))
    if content:
        claims.append(FieldClaim(field="content", value=content, source=source, weight=0.9))
    if extra_claims:
        claims.extend(extra_claims)
    return {"raw_title": title, "claims": claims, "url": "https://x/" + title[:10], "raw": {}}


def ak_cfg():
    return GameConfig.from_dict("arknights", {
        "display": "方舟", "theme_color": "#333C44", "format": "activity_based",
        "adapter": "hg_json", "adapter_params": {}, "activity_mode": True,
        "title_include": ["复刻", "寻访", "通讯"], "cycle_days": 28,
    })


def hsr_cfg(with_known=True):
    kd = {"preview_time": "2026-08-14 19:30", "next_version": "4.5", "next_name": "挥掷千星的筹码"} if with_known else {}
    return GameConfig.from_dict("hsr", {
        "display": "崩铁", "theme_color": "#6A5AE0", "format": "version_based",
        "adapter": "mihoyo_json", "adapter_params": {}, "cycle_days": 42, "half_days": 21,
        "version_pattern": "(?P<num>[\\d.]+)版本「(?P<name>.+?)」",
        "title_include": ["版本更新"],
        "preview_sources": {"title_include": ["前瞻", "特别节目"]},
        "known_dates": kd,
    })


def ef_cfg():
    return GameConfig.from_dict("endfield", {
        "display": "终末地", "theme_color": "#2F6F4F", "format": "version_based",
        "adapter": "hg_ssr", "adapter_params": {}, "cycle_days": 42, "half_days": 21,
        "version_pattern": "「(?P<name>.+?)」版本更新说明",
        "title_include": ["版本更新说明"],
        "preview_sources": {"title_include": ["研发通讯"]},
        "known_dates": {"next_name": "雪凇幽梦", "next_update_time": "2026-08-26", "next_characters": "伊冯"},
    })


def hsr_main():
    return item("4.4版本「鸣笛于归寂之时」版本更新说明", "4.4版本内容",
                extra_claims=[FieldClaim(field="start_time", value="2026-07-15 06:00:00", source="mihoyo_json", weight=1.0)],
                source="mihoyo_json")


async def main() -> None:
    pl = UpdatePipeline()

    # ---------- R1 H1 自指守卫 ----------
    cfg_ef = ef_cfg()
    ef_main = item("「雪凇幽梦」版本更新说明", "雪凇幽梦版本内容",
                   extra_claims=[FieldClaim(field="update_time", value="2026-08-26", source="hg_ssr", weight=1.0)],
                   source="hg_ssr")
    ef_pv = item("「雪凇幽梦」版本研发通讯", "「雪凇幽梦」版本即将开启，重构寻访9月24日开启")
    u = (await pl.build_updates(cfg_ef, [ef_main, ef_pv], 0.8))[0]
    check("R1 H1 自指守卫：next_name 不注入当前版本名", u.field_value("next_name") == "" and u.version_name == "雪凇幽梦",
          f"next_name={u.field_value('next_name')!r}")

    # ---------- R2 版本制空锚点筛选 ----------
    cfg_h = hsr_cfg(with_known=False)
    no_anchor = item("2.0版本「无时间锚点版本」版本更新说明", "内容", source="mihoyo_json")
    anchored = hsr_main()
    updates = await pl.build_updates(cfg_h, [no_anchor, anchored], 0.8)
    check("R2 空锚点筛选：有锚点条目胜出", len(updates) == 1 and updates[0].version_name == "鸣笛于归寂之时",
          f"{[(u.version_name, u.field_value('update_time')) for u in updates]}")
    # 全空锚点：不崩、保留候选
    updates2 = await pl.build_updates(cfg_h, [no_anchor], 0.8)
    check("R2 全空锚点：保留候选不崩", len(updates2) == 1, str(len(updates2)))

    # ---------- R3 来源标注 ----------
    u = (await pl.build_updates(cfg_h, [hsr_main()], 0.8))[0]
    tl = build_timeline(u, cfg_h, date(2026, 8, 25))  # 7/15+42 → stage6，下版本纯推算
    check("R3 stage6 推算槽 source=estimate", all(s.source == "estimate" for s in tl.slots),
          str([(s.label, s.source) for s in tl.slots]))
    # 宽松预估分支（无 update_time）
    u_none = (await pl.build_updates(cfg_h, [no_anchor], 0.8))[0]
    tl2 = build_timeline(u_none, cfg_h, date(2026, 8, 25))
    check("R3 宽松预估分支 source=estimate", tl2.slots[1].source == "estimate" and tl2.slots[1].estimated,
          str([(s.label, s.source) for s in tl2.slots]))

    # ---------- R4 方舟事件状态机 ----------
    cfg_a = ak_cfg()
    cands_a = [
        item("【墟】复刻即将开启",
             "一、SideStory「墟」限时复刻开启\n活动时间：08月22日 04:00 - 09月05日 03:59\n主要奖励：活动干员【★★★★★：松桐】",
             display_time="2026-08-17", source="hg_json"),
        item("中坚甄选\n限时寻访开启", display_time="2026-08-20", source="hg_json"),
        item("「制作组通讯」#68期",
             "【通讯节点·一】SideStory「月行水上」限时活动将于9月上旬开启\n【通讯节点·二】「逐影集趣」活动将于9月中下旬开启",
             display_time="2026-08-21", source="hg_json"),
        item("「夏日嘉年华」活动限时开启",
             "一、「夏日嘉年华」，SideStory「直到大地变成一颗酸橙」活动开启\n活动时间：08月01日 12:00 - 08月22日 03:59",
             display_time="2026-07-25", source="hg_json"),
    ]
    u = (await pl.build_updates(cfg_a, cands_a, 0.8))[0]
    evs = json.loads(u.field_value("events"))
    kinds = {ev["name"]: ev["kind"] for ev in evs}
    check("R4a active 主事件=墟 + banner + roadmap",
          u.version_name == "墟" and kinds.get("中坚甄选") == "banner" and kinds.get("月行水上") == "roadmap",
          str(kinds))
    check("R4a banner 池注入", u.field_value("banner_name") == "中坚甄选", u.field_value("banner_name"))
    tl = build_timeline(u, cfg_a, date(2026, 8, 25))
    check("R4a 栏位A官方日期+栏位B roadmap 粗窗口",
          tl.slots[0].source == "official" and tl.slots[1].date == "9月上旬" and tl.slots[1].source == "preview",
          str([(s.main, s.date, s.source) for s in tl.slots]))
    # upcoming
    u2 = (await pl.build_updates(cfg_a, cands_a[:2], 0.8))[0]
    tl = build_timeline(u2, cfg_a, date(2026, 8, 20))
    check("R4b upcoming 分支", tl.slots[0].label == "下版本·即将开启" and tl.slots[0].main == "「墟」",
          str([(s.label, s.main) for s in tl.slots]))
    # 空窗
    u3 = (await pl.build_updates(cfg_a, [item("「制作组通讯」#66期", "【通讯节点】内容", display_time="2026-07-10", source="hg_json")], 0.8))[0]
    tl = build_timeline(u3, cfg_a, date(2026, 8, 25))
    check("R4c 空窗占位（中性 source，无小标）",
          tl.slots[0].main == "暂无活动" and tl.slots[0].source == "",
          str([(s.main, s.source) for s in tl.slots]))
    # 并行活动：两个 active → 栏位B=并行
    cands_par = [
        item("【墟】复刻即将开启",
             "一、SideStory「墟」限时复刻开启\n活动时间：08月22日 04:00 - 09月05日 03:59",
             display_time="2026-08-17", source="hg_json"),
        item("「夏日嘉年华」活动限时开启",
             "一、「夏日嘉年华」，SideStory「直到大地变成一颗酸橙」活动开启\n活动时间：08月20日 04:00 - 09月05日 03:59",
             display_time="2026-07-25", source="hg_json"),
    ]
    u4 = (await pl.build_updates(cfg_a, cands_par, 0.8))[0]
    tl = build_timeline(u4, cfg_a, date(2026, 8, 25))
    check("R4d 并行活动栏位B", tl.slots[1].label == "并行活动" and tl.slots[1].source == "official",
          str([(s.label, s.main) for s in tl.slots]))

    # ---------- R5 LLM 兜底护栏 ----------
    calls: list[str] = []

    async def mock_llm(title, content):
        calls.append(title)
        if "越界" in title:
            return {"next_update_time": "2026-10-20", "next_name": "越界版本"}
        return {"next_update_time": "2026-08-26", "next_name": "提振版本", "next_characters": ["新角色甲"]}

    pl.llm_fallback = mock_llm
    u = (await pl.build_updates(cfg_h, [hsr_main(), item("前瞻情报预告", "下版本将于9月3日更新")], 0.8))[0]
    check("R5a LLM 合法注入 conf=0.8/每轮1次",
          len(calls) == 1 and u.field_value("next_update_time") == "2026-08-26" and u.fields["next_update_time"].confidence == 0.8
          and u.field_value("next_name") == "提振版本" and "新角色甲" in u.field_value("next_characters"),
          f"calls={len(calls)}")
    calls.clear()
    u = (await pl.build_updates(cfg_h, [hsr_main(), item("越界前瞻预告", "下版本将于9月30日开启")], 0.8))[0]
    check("R5b 越界时间拒绝（偏差55天）", len(calls) == 1 and u.field_value("next_update_time") == "",
          f"next_update_time={u.field_value('next_update_time')!r}")
    calls.clear()
    u = (await pl.build_updates(cfg_h, [hsr_main(), item("4.6版本「新世界之旅」前瞻特别节目将于2026年10月1日19:00直播", "将于2026年10月1日19:00直播")], 0.8))[0]
    check("R5c 正则命中零 LLM 调用", len(calls) == 0 and u.field_value("preview_time") == "2026-10-01 19:00",
          f"calls={len(calls)} preview={u.field_value('preview_time')!r}")
    pl.llm_fallback = None

    # ---------- R6 known 合并去重（伊冯案例） ----------
    ef_pv_full = item("「雪凇幽梦」版本研发通讯",
                      "「绚丽异彩」重构寻访#1将于2026年9月24日开启，活动期间6星干员【伊冯】获取概率大幅提升")
    u = (await pl.build_updates(ef_cfg(), [ef_main, ef_pv_full], 0.8))[0]
    check("R6 伊冯仅在下半池（上半名单剔除）",
          u.field_value("next_characters") == "" and u.field_value("next_half_characters") == "伊冯",
          f"up={u.field_value('next_characters')!r} half={u.field_value('next_half_characters')!r}")

    # ---------- R7 预告插值识别（C-M2 回归） ----------
    interp = item("SideStory「墟」复刻即将于8月22日04:00开启！",
                  "SideStory「墟」限时复刻开启\n活动时间：08月22日 04:00 - 09月05日 03:59",
                  source="bili_dynamic")
    cfg_a2 = ak_cfg()
    # 该动态应为预告类：extract_fields 对 activity_mode 正文执行 m_at（活动时间优先）
    from guw_core.validator import extract_fields  # noqa: E402
    claims = extract_fields(interp, cfg_a2)
    check("R7 插值形态识别为预告并提取活动时间",
          any(c.field == "next_activity_start" and c.value == "2026-08-22" for c in claims),
          str([(c.field, c.value) for c in claims if c.field in ("next_activity_start", "update_time")]))

    # ---------- R8 RE_HALF_CHAR 长名单 ----------
    u = (await pl.build_updates(hsr_cfg(with_known=False), [
        item("4.4版本「鸣笛于归寂之时」版本更新说明",
             "4.4版本内容 全新角色 5星「测试」\n返回角色刻律德菈、那刻夏、砂金、姬子、开拓者、三月七、丹恒、希露瓦、娜塔莎、青雀则将于下半回归跃迁",
             extra_claims=[FieldClaim(field="start_time", value="2026-07-15 06:00:00", source="mihoyo_json", weight=1.0)],
             source="mihoyo_json"),
    ], 0.8))[0]
    half = u.field_value("half_characters")
    check("R8 长名单提取>24字符不截断", "三月七" in half and len(half) > 24,
          f"half={half!r} len={len(half)}")

    # 汇总
    print(f"\n===== 离线回归完成: {_PASS} PASS / {_FAIL} FAIL =====")
    if _FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())