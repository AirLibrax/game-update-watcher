"""自测脚本：不依赖 MaiBot，直接验证 采集→解析→认证→出图 全链路。

用法（在插件目录内）：
    python selftest.py                 # 全部游戏
    python selftest.py wuwa hsr        # 只测指定游戏（用 games/ 下的文件名）

依赖：httpx, pillow
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from guw_core.models import GameConfig
from guw_core.pipeline import UpdatePipeline
from guw_core.renderer import render_card, render_summary
from guw_core.timeline import build_timeline

OUT_DIR = Path("_selftest_out")


async def main() -> None:
    targets = set(sys.argv[1:])
    pipeline = UpdatePipeline(logger=None)
    games_dir = Path(__file__).parent / "games"
    cfgs = pipeline.load_games(games_dir)

    if targets:
        cfgs = {k: v for k, v in cfgs.items() if k in targets}
    if not cfgs:
        print("没有匹配的游戏配置")
        return

    # 收集所有游戏的时间线，最后生成汇总图
    all_entries: list[tuple] = []
    for cfg in cfgs.values():
        up_list = await collect_entries(pipeline, cfg, threshold=0.8)
        all_entries.extend(up_list)

    # 数据源状态（P1-3）
    st = getattr(pipeline, "collect_status", None) or {}
    if st:
        parts = []
        for key, s in st.items():
            m = {"ok": "✓", "empty": "·", "fail": "✗"}.get(s.get("main"), "·")
            b = s.get("bili")
            if b is None:
                parts.append(f"{key}{m}")
            else:
                parts.append(f"{key}{m}站{'✓' if b == 'ok' else ('~' if b == 'empty' else '✗')}")
        print(f"\n[数据源状态] {' '.join(parts)}")

    if all_entries:
        OUT_DIR.mkdir(exist_ok=True)
        try:
            out = render_summary(all_entries, OUT_DIR / "summary.png")
            print(f"\n[汇总图] {out} ({len(all_entries)} 个条目)")
        except Exception as e:
            print(f"\n[汇总图失败] {e}")


async def collect_entries(pipeline: UpdatePipeline, cfg: GameConfig, threshold: float) -> list[tuple]:
    """单游戏采集+解析+出图，返回 (update, cfg, timeline) 列表供汇总图使用。"""
    entries: list[tuple] = []
    print(f"\n===== {cfg.display} (adapter: {cfg.adapter}) =====")
    try:
        candidates = await pipeline.collect_game(cfg, timeout=15.0)
    except Exception as e:
        print(f"  [采集失败] {e}")
        return entries
    if not candidates:
        print("  无命中标题规则的条目")
        return entries
    print(f"  命中 {len(candidates)} 条:")
    for it in candidates[:8]:
        print(f"    - {it['raw_title'][:60]}")

    updates = await pipeline.build_updates(cfg, candidates, threshold)
    print(f"  解析出 {len(updates)} 个版本条目:")
    for up in updates[:5]:
        print(f"    * {up.display_title}  key={up.dedup_key}")
        for field, v in up.fields.items():
            if field in ("raw_title", "content"):
                continue
            flag = " ⚠️待确认" if v.pending else ""
            print(f"        {field:12s} = {v.value[:50]}{flag}  conf={v.confidence:.2f}")

    # 出图
    OUT_DIR.mkdir(exist_ok=True)
    for up in updates[:3]:
        try:
            tl = build_timeline(up, cfg)
            print(f"  [时间线] 阶段判定: {len(tl.slots)} 栏位")
            for s in tl.slots:
                tag = " (预估)" if s.estimated else ""
                print(f"      - [{s.label}] {s.main} | {s.date}{tag} | {s.chars}")
            out = render_card(up, cfg, tl, OUT_DIR / f"{cfg.key}_sample.png")
            print(f"  [出图] {out}")
            entries.append((up, cfg, tl))
        except Exception as e:
            print(f"  [出图失败] {e}")
    return entries


if __name__ == "__main__":
    asyncio.run(main())
