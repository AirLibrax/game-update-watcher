"""核心管道：采集 → 解析 → 交叉认证 → 出图 → 发送。

把 adapter / validator / renderer / store 串成一次「轮询单游戏」的流程。
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from core.adapters import create_adapter
from core.models import FieldVerdict, GameConfig, GameUpdate
from core.renderer import render_card
from core.store import PublishStore
from core.timeline import build_timeline
from core.validator import aggregate, extract_fields


class UpdatePipeline:
    def __init__(self, logger=None, store: PublishStore | None = None):
        self.logger = logger
        self.store = store

    def _log(self, msg: str) -> None:
        if self.logger:
            self.logger.info(msg)

    def load_games(self, games_dir: Path) -> dict[str, GameConfig]:
        """加载 games/*.json 为配置。"""
        cfgs: dict[str, GameConfig] = {}
        for f in sorted(games_dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                cfgs[f.stem] = GameConfig.from_dict(f.stem, d)
            except Exception as e:
                self._log(f"[配置] 加载 {f.name} 失败: {e}")
        return cfgs

    def _match_rules(self, title: str, cfg: GameConfig) -> bool:
        """标题命中规则：必须含 include 之一，且不含 exclude。"""
        inc = cfg.extra.get("title_include", [])
        exc = cfg.extra.get("title_exclude", [])
        if inc and not any(k in title for k in inc):
            return False
        if any(k in title for k in exc):
            return False
        return True

    async def collect_game(self, cfg: GameConfig, timeout: float = 15.0) -> list[dict[str, Any]]:
        """采集 + 筛选出候选条目（命中标题规则的）。"""
        adapter = create_adapter(cfg.adapter, {**cfg.adapter_params, "timeout": timeout}, self.logger)
        items = await adapter.collect()
        cands = [it for it in items if self._match_rules(it.get("raw_title", ""), cfg)]
        self._log(f"[{cfg.display}] 采集 {len(items)} 条，命中规则 {len(cands)} 条")
        return cands

    async def build_updates(self, cfg: GameConfig, candidates: list[dict[str, Any]],
                            publish_threshold: float) -> list[GameUpdate]:
        """候选条目 → 字段提取 → 交叉认证 → GameUpdate 列表。"""
        updates: list[GameUpdate] = []
        half_starts: list[str] = []  # 卡池公告提供的官方下半池时间
        for item in candidates:
            claims = extract_fields(item, cfg)
            # 卡池公告（无版本名，只有 half_start）：不进卡片列表，仅收集时间
            hs = [c.value for c in claims if c.field == "half_start" and c.value]
            if hs:
                half_starts.extend(hs)
                continue
            verdicts = aggregate(claims, publish_threshold)
            if "version_name" not in verdicts:
                continue
            v = verdicts["version_name"].value
            num = verdicts.get("version")
            update = GameUpdate(
                game=cfg.key,
                game_display=cfg.display,
                version_num=num.value if num else None,
                version_name=v,
                fields=verdicts,
                raw_urls=[item.get("url", "")],
                raw_title=item.get("raw_title", ""),
                collected_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )
            updates.append(update)

        # 把卡池公告的官方下半池时间注入主版本条目
        if half_starts and updates:
            updates[0].fields["half_start"] = FieldVerdict(
                field="half_start", value=half_starts[0], confidence=1.0, sources=["parse"]
            )

        if cfg.activity_mode and len(updates) > 1:
            # 活动制（方舟）：多条公告命中时合并。
            # main = 主活动（SideStory/嘉年华/活动开启类），
            # next = 下版本预告（复刻即将开启类公告，如【红丝绒】复刻即将开启）
            def _priority(u: GameUpdate) -> tuple[int, int]:
                # 用原始标题判断（提取后的 SideStory 名可能不含"嘉年华"等关键词）
                rt = u.raw_title
                if any(k in rt for k in ("SideStory", "嘉年华", "活动")):
                    return (0, -len(u.field_value("characters")))
                return (1, -len(u.field_value("characters")))

            updates.sort(key=_priority)
            main = updates[0]

            # 复刻标记：只看主活动自身的原始标题（候选里可能有下版本复刻预告，不能误伤当前版本）
            if "复刻" in main.raw_title:
                main.fields["is_reprint"] = FieldVerdict(field="is_reprint", value="1", confidence=1.0, sources=["parse"])

            # 下版本预告：找标题含"复刻"且非时装/皮肤/周边类的公告
            next_name = ""
            next_is_reprint = False
            for it in candidates:
                rt = it.get("raw_title", "")
                if any(k in rt for k in ("时装", "皮肤", "周边", "模组")):
                    continue
                if "复刻" in rt and "即将开启" in rt:
                    m = re.search(r"[【\[]([^】\]]+)[】\]]", rt)
                    if m:
                        next_name = m.group(1).strip()
                    else:
                        next_name = rt.replace("复刻", "").replace("即将开启", "").strip()
                    next_is_reprint = True
                    break
            if next_name:
                main.fields["next_activity"] = FieldVerdict(
                    field="next_activity", value=next_name, confidence=1.0, sources=["parse"]
                )
                main.fields["next_is_reprint"] = FieldVerdict(
                    field="next_is_reprint", value="1" if next_is_reprint else "0", confidence=1.0, sources=["parse"]
                )

            # 合并所有候选的角色（主活动+当期寻访说明），去重保序
            # 排除"复刻即将开启"类候选（那是下版本预告，角色属于下版本）
            merged: list[str] = []
            for u in updates:
                if "复刻即将开启" in u.raw_title:
                    continue
                c = u.field_value("characters")
                for nm in [x.strip() for x in c.split(",") if x.strip()]:
                    if nm and nm not in merged:
                        merged.append(nm)
            if merged:
                main.fields["characters"] = FieldVerdict(
                    field="characters", value=",".join(merged), confidence=1.0, sources=["parse"]
                )
            updates = [main]
        return updates

    def render(self, update: GameUpdate, cfg: GameConfig, runtime_dir: Path,
               watermark: str = "") -> Path:
        """出图，返回 PNG 路径。"""
        timeline = build_timeline(update, cfg)
        safe_name = re.sub(r"[^\w\u4e00-\u9fff]", "_", update.display_title)[:40]
        out = runtime_dir / f"{cfg.key}_{safe_name}.png"
        return render_card(update, cfg, timeline, out, watermark=watermark)

    def encode_image(self, png_path: Path) -> str:
        """PNG → base64（send.image 需要）。"""
        return base64.b64encode(png_path.read_bytes()).decode()

    def is_new(self, update: GameUpdate) -> bool:
        return not (self.store and self.store.is_published(update.dedup_key))

    def mark_sent(self, update: GameUpdate) -> None:
        if self.store:
            self.store.mark_published(
                update.game, update.version_num, update.version_name,
                update.dedup_key,
                payload=json.dumps({k: v.value for k, v in update.fields.items()}, ensure_ascii=False),
            )
