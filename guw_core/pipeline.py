"""核心管道：采集 → 解析 → 交叉认证 → 出图 → 发送。

把 adapter / validator / renderer / store 串成一次「轮询单游戏」的流程。
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from guw_core.adapters import create_adapter
from guw_core.models import FieldVerdict, GameConfig, GameUpdate
from guw_core.store import PublishStore
from guw_core.validator import aggregate, extract_fields, extract_preview_claims


def _to_date(s: str) -> date | None:
    """解析 'YYYY-MM-DD...' 前缀为 date（timeline.parse_date 的轻量版，避免跨模块依赖）。"""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return None


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
        """标题命中规则：必须含 include（或 preview_sources 的 include）之一，且不含 exclude。

        preview_sources 命中的条目代表"预告类公告"（如研发通讯、活动说明动态），
        后续在 build_updates 里走预告池，不生成独立 GameUpdate。
        """
        inc = cfg.extra.get("title_include", [])
        exc = cfg.extra.get("title_exclude", [])
        pre = list((cfg.preview_sources or {}).get("title_include", []))
        if inc and not any(k in title for k in inc) and not any(k in title for k in pre):
            return False
        if any(k in title for k in exc):
            return False
        return True

    def _is_preview_item(self, title: str, cfg: GameConfig) -> bool:
        """是否命中 preview_sources 规则（= 预告类公告，不生成 GameUpdate，只抽 next_*）。"""
        pre = list((cfg.preview_sources or {}).get("title_include", []))
        return bool(pre) and any(k in title for k in pre)

    async def collect_game(self, cfg: GameConfig, timeout: float = 15.0) -> list[dict[str, Any]]:
        """采集主源 + 额外认证源，合并所有条目（命中标题规则的）。

        额外源（如 B站动态）的条目会作为同字段的第二声明，
        aggregate 据此做字段级交叉认证（多源一致 → 置信度提升）。
        """
        items: list[dict[str, Any]] = []
        # 主源
        adapter = create_adapter(cfg.adapter, {**cfg.adapter_params, "timeout": timeout}, self.logger)
        try:
            items.extend(await adapter.collect())
        except Exception as e:
            self._log(f"[{cfg.display}] 主源 {cfg.adapter} 采集失败: {e}")
        # 额外认证源
        for src in cfg.extra_sources:
            try:
                extra = create_adapter(src["adapter"], {**src.get("params", {}), "timeout": timeout}, self.logger)
                items.extend(await extra.collect())
            except Exception as e:
                self._log(f"[{cfg.display}] 认证源 {src.get('adapter')} 采集失败: {e}")

        cands = [it for it in items if self._match_rules(it.get("raw_title", ""), cfg)]
        self._log(f"[{cfg.display}] 采集 {len(items)} 条（主源+认证源），命中规则 {len(cands)} 条")
        return cands

    async def build_updates(self, cfg: GameConfig, candidates: list[dict[str, Any]],
                            publish_threshold: float) -> list[GameUpdate]:
        """候选条目 → 字段提取 → 交叉认证 → GameUpdate 列表。

        多源逻辑：
        - 主源条目（版本更新说明等）正常生成 GameUpdate
        - 认证源条目（如 B站动态）不生成独立条目，而是按版本号匹配主条目后，
          把其字段声明注入主条目的 claim 列表重新聚合，实现字段级交叉认证
        """
        # 第一遍：主源条目 → 生成 GameUpdate；认证源条目 → 暂存等待匹配；预告类条目 → 暂存抽 next_*
        main_items: list[dict[str, Any]] = []      # 主源原始条目
        auth_items: list[dict[str, Any]] = []      # 认证源原始条目
        preview_items: list[dict[str, Any]] = []   # 预告类原始条目（preview_sources 命中）
        half_starts: list[str] = []                # 卡池公告提供的官方下半池时间

        for item in candidates:
            claims = extract_fields(item, cfg)
            # 卡池公告（无版本名，只有 half_start）：不进卡片列表，仅收集时间
            # 终末地的版本更新说明也含 half_start，但它本身是主条目，不能按卡池公告跳过
            has_name = any(c.field == "version_name" and c.value for c in claims)
            hs = [c.value for c in claims if c.field == "half_start" and c.value]
            if hs and not has_name:
                half_starts.extend(hs)
                continue
            # 预告类（研发通讯/前瞻/B站活动说明动态）→ 预告池：不生成 GameUpdate，只贡献 next_*
            if self._is_preview_item(item.get("raw_title", ""), cfg):
                preview_items.append(item)
                continue
            # 判断是否为认证源：看原始 adapter 的 claims（提取后会丢失 source 标记）
            raw_sources = {c.source for c in item.get("claims", [])}
            if any("bili" in s for s in raw_sources):
                auth_items.append(item)
            else:
                main_items.append(item)

        # 主源 → GameUpdate
        updates: list[GameUpdate] = []
        # update id → 其主源条目提取的 claims（用 id 关联：版本筛选后 updates 会重建，索引会错位）
        main_claims_map: dict[int, list] = {}
        for idx, item in enumerate(main_items):
            claims = extract_fields(item, cfg)
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
            main_claims_map[id(update)] = claims
            updates.append(update)

        # 版本制：新旧版本更新说明并存时只保留"最新已开始"的主条目（防汇总图同一游戏双块）
        # 例：崩铁 4.5 上线后，4.4 与 4.5 更新说明同时在候选池 → 取 update_time 最新且 ≤ today 者
        if updates and not cfg.activity_mode and len(updates) > 1:
            today_s = datetime.now().strftime("%Y-%m-%d")
            started = [u for u in updates if (u.field_value("update_time") or "")[:10] <= today_s]
            if started:
                updates = [max(started, key=lambda u: u.field_value("update_time"))]
            else:
                # 全部未开始（异常：未来版本的说明先于当期出现）→ 取最早者
                updates = [min(updates, key=lambda u: u.field_value("update_time"))]

        # 认证源匹配：优先按活动名匹配，没有活动名再按版本号
        for auth in auth_items:
            auth_claims = extract_fields(auth, cfg)
            auth_name, auth_ver = self._auth_match_key(auth, cfg, auth_claims)
            if not auth_name and not auth_ver:
                continue
            target_idx = None
            match_by = ""
            # 优先活动名（版本名）匹配
            if auth_name:
                for i, up in enumerate(updates):
                    if up.version_name and up.version_name == auth_name:
                        target_idx = i
                        match_by = f"活动「{auth_name}」"
                        break
            # 版本号兜底
            if target_idx is None and auth_ver:
                for i, up in enumerate(updates):
                    if up.version_num and up.version_num == auth_ver:
                        target_idx = i
                        match_by = f"v{auth_ver}"
                        break
            if target_idx is None:
                continue
            # 认证源贡献时间/角色类字段；版本标识（version/version_name）以主源为准，避免乱文覆盖
            auth_claims = [c for c in auth_claims if c.field not in ("version", "version_name")]
            if not auth_claims:
                continue
            # 主源 claims + 认证源 claims 一起聚合 → 同字段多源加权
            base_claims = main_claims_map.get(id(updates[target_idx]), [])
            merged_claims = base_claims + auth_claims
            verdicts = aggregate(merged_claims, publish_threshold)
            updates[target_idx].fields = verdicts
            self._log(f"[{cfg.display}] 认证源匹配 {match_by}，合并 {len(auth_claims)} 条字段声明")

        # 把卡池公告的官方下半池时间注入主版本条目
        if half_starts and updates:
            updates[0].fields["half_start"] = FieldVerdict(
                field="half_start", value=half_starts[0], confidence=1.0, sources=["parse"]
            )

        # known_dates 生命周期：版本切换后，仍指向"当前版本"的 next_* 与旧前瞻时间不再注入
        # 例：4.5 上线后 known_dates 的 next_version=4.5 == 主条目版本号 → 清理，防止旧值冒充下版本
        if updates and not cfg.activity_mode:
            up = updates[0]
            if up.version_num and up.field_value("next_version") == up.version_num:
                for f in ("next_name", "next_version", "next_characters", "preview_time"):
                    up.fields.pop(f, None)

        # 预告类公告（preview_sources，如终末地研发通讯/B站活动说明动态）→ next_* 注入主条目
        # 首项优先（主源条目在前）：先注入的 next_name 不被后到的活动名覆盖；已有官方/known 值不覆盖
        if preview_items and updates:
            main_upd = updates[0]
            main_start = _to_date(main_upd.field_value("update_time")) or date.today()
            for pit in preview_items:
                for claim in extract_preview_claims(pit, cfg, main_start):
                    if not main_upd.field_value(claim.field):
                        main_upd.fields[claim.field] = FieldVerdict(
                            field=claim.field, value=claim.value, confidence=1.0, sources=["parse"]
                        )

        if cfg.activity_mode and updates:
            # 活动制（方舟）：事件模型 —— 从候选里选出主活动。
            # 主事件 = 进行中的活动中最新开始的（预告升格：预告公告的活动开始日 ≤ today 即升格为主，
            # 解决"酸橙已结束仍占位、墟进行中却被当预告"的问题）；
            # 无进行中活动时退回旧优先级逻辑（SideStory/嘉年华/活动 优先）。
            def _priority(u: GameUpdate) -> tuple[int, int]:
                # 用原始标题判断（提取后的 SideStory 名可能不含"嘉年华"等关键词）
                rt = u.raw_title
                if any(k in rt for k in ("SideStory", "嘉年华", "活动")):
                    return (0, -len(u.field_value("characters")))
                return (1, -len(u.field_value("characters")))

            def _active(u: GameUpdate) -> bool:
                s = _to_date(u.field_value("update_time"))
                e = _to_date(u.field_value("activity_end"))
                return bool(s and e and s <= date.today() <= e)

            active = [u for u in updates if _active(u)]
            if active:
                main = max(active, key=lambda u: u.field_value("update_time"))
            else:
                main = sorted(updates, key=_priority)[0]

            # 当前活动开始日（用于过滤历史复刻预告）
            start_date_str = main.field_value("update_time") or ""

            # 复刻标记：只看主活动自身的原始标题（候选里可能有下版本复刻预告，不能误伤当前版本）
            if "复刻" in main.raw_title:
                main.fields["is_reprint"] = FieldVerdict(field="is_reprint", value="1", confidence=1.0, sources=["parse"])

            # 下版本预告：找标题含"复刻"且非时装/皮肤/周边类的公告
            # 关键：只接受发布时间晚于当前活动开始日的公告（bulletinList 返回全量历史，需过滤过期预告）
            next_name = ""
            next_is_reprint = False
            next_activity_start = ""
            next_activity_end = ""
            for it in candidates:
                rt = it.get("raw_title", "")
                if any(k in rt for k in ("时装", "皮肤", "周边", "模组")):
                    continue
                if "复刻" in rt and "即将开启" in rt:
                    # 时间过滤：公告 displayTime 必须 >= 当前活动开始日，否则是历史复刻预告
                    claim_dt = next((c.value for c in it.get("claims", []) if c.field == "display_time" and c.value), "")
                    if claim_dt and claim_dt < start_date_str:
                        continue
                    m = re.search(r"[【\[]([^】\]]+)[】\]]", rt)
                    if m:
                        next_name = m.group(1).strip()
                    else:
                        next_name = rt.replace("复刻", "").replace("即将开启", "").strip()
                    next_is_reprint = True
                    # 预告正文活动时间 → 官方下版本起止（validator 对"即将开启"类公告已映射 next_activity_*）
                    pre_claims = extract_fields(it, cfg)
                    next_activity_start = next((c.value for c in pre_claims if c.field == "next_activity_start" and c.value), "")
                    next_activity_end = next((c.value for c in pre_claims if c.field == "next_activity_end" and c.value), "")
                    break
            if next_name:
                main.fields["next_activity"] = FieldVerdict(
                    field="next_activity", value=next_name, confidence=1.0, sources=["parse"]
                )
                main.fields["next_is_reprint"] = FieldVerdict(
                    field="next_is_reprint", value="1" if next_is_reprint else "0", confidence=1.0, sources=["parse"]
                )
                if next_activity_start:
                    main.fields["next_activity_start"] = FieldVerdict(
                        field="next_activity_start", value=next_activity_start, confidence=1.0, sources=["parse"]
                    )
                if next_activity_end:
                    main.fields["next_activity_end"] = FieldVerdict(
                        field="next_activity_end", value=next_activity_end, confidence=1.0, sources=["parse"]
                    )

            # 角色：只保留主活动自身的（历史活动/下版本预告的角色不得混入当期）
            merged: list[str] = []
            for nm in [x.strip() for x in main.field_value("characters").split(",") if x.strip()]:
                if nm and nm not in merged:
                    merged.append(nm)
            if merged:
                main.fields["characters"] = FieldVerdict(
                    field="characters", value=",".join(merged), confidence=1.0, sources=["parse"]
                )
            updates = [main]
        return updates

    def _auth_match_key(self, item: dict, cfg: GameConfig, auth_claims: list | None = None) -> tuple[str, str]:
        """从认证源条目提取匹配键：(活动名, 版本号)。

        活动名优先（B站动态标题如「向渊行」版本更新说明 可提取出 '向渊行'），
        版本号兜底（如 "4.4版本活动跃迁（其二）" 提取 '4.4'）。
        活动名取 extract_fields 提取的 version_name，并清洗掉乱文
        （B站动态可能把角色名或整段正文当 version_name，需过滤）。
        """
        claims = auth_claims if auth_claims is not None else extract_fields(item, cfg)
        name = ""
        for c in claims:
            if c.field == "version_name" and c.value:
                candidate = c.value.strip()
                # 清洗：剥离 #话题# 前缀（B站动态标题常见），再判断是否乱文
                cleaned = re.sub(r"#\S+?#", "", candidate).strip()
                if (
                    "\n" in cleaned
                    or len(cleaned) > 20
                    or any(k in cleaned for k in ("亲爱的", "开拓者", "管理员", "博士", "漂泊者", "大家好", "欢迎"))
                ):
                    continue
                name = cleaned
                break
        ver = ""
        m = re.search(r"(\d+\.\d+)\s*版本", item.get("raw_title", ""))
        if m:
            ver = m.group(1)
        return name, ver

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
