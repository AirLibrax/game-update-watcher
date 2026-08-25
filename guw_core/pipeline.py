"""核心管道：采集 → 解析 → 交叉认证 → 出图 → 发送。

把 adapter / validator / renderer / store 串成一次「轮询单游戏」的流程。
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from guw_core.adapters import create_adapter
from guw_core.models import FieldVerdict, GameConfig, GameUpdate
from guw_core.store import PublishStore
from guw_core.validator import aggregate, extract_fields, extract_preview_claims, extract_roadmap_nodes


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
        # 数据源状态（P1-3）：game_key -> {"main": ok|empty|fail, "bili": ok|empty|fail|None}
        self.collect_status: dict[str, dict[str, str | None]] = {}
        # P2-1 LLM 兜底回调（由插件注入，带 ctx.llm）：async (title, content) -> dict | None
        self.llm_fallback: Callable | None = None

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
        数据源状态写入 self.collect_status[game_key]（P1-3 可观测性）。
        """
        items: list[dict[str, Any]] = []
        status: dict[str, str | None] = {"main": "fail", "bili": None}
        # 主源
        adapter = create_adapter(cfg.adapter, {**cfg.adapter_params, "timeout": timeout}, self.logger)
        try:
            got = await adapter.collect()
            items.extend(got)
            status["main"] = "ok" if got else "empty"
        except Exception as e:
            self._log(f"[{cfg.display}] 主源 {cfg.adapter} 采集失败: {e}")
        # 额外认证源（每源独立容错：一个账号风控不拖累其他游戏/其他源）
        for src in cfg.extra_sources:
            try:
                extra = create_adapter(src["adapter"], {**src.get("params", {}), "timeout": timeout}, self.logger)
                got = await extra.collect()
                items.extend(got)
                if "bili" in src.get("adapter", ""):
                    status["bili"] = "ok" if got else "empty"  # empty=风控/无内容，静默降级
            except Exception as e:
                self._log(f"[{cfg.display}] 认证源 {src.get('adapter')} 采集失败: {e}")
                if "bili" in src.get("adapter", ""):
                    status["bili"] = "fail"

        self.collect_status[cfg.key] = status
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
        today_s = datetime.now().strftime("%Y-%m-%d")

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
        # B-M4 修复：update_time 为空的条目（无锚点）不参与 "≤ today" 比较（空串恒 ≤ 任何日期）
        if updates and not cfg.activity_mode and len(updates) > 1:

            def _anchor(u: GameUpdate) -> str:
                return (u.field_value("update_time") or "")[:10]

            started = [u for u in updates if _anchor(u) and _anchor(u) <= today_s]
            if started:
                updates = [max(started, key=_anchor)]
            else:
                anchored = [u for u in updates if _anchor(u)]
                if anchored:
                    # 全部未开始（异常：未来版本的说明先于当期出现）→ 取有锚点的最早者
                    updates = [min(anchored, key=_anchor)]
                # 全无锚点（异常：均无 update_time）→ 保留原候选顺序，不做筛选

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
        # 例1：4.5 上线后 known_dates 的 next_version=4.5 == 主条目版本号 → 清理
        # 例2：无版本号游戏（终末地）版本切换后 known next_name == 主条目版本名 → 同样清理（自指残留）
        if updates and not cfg.activity_mode:
            up = updates[0]
            stale_known = (
                (up.version_num and up.field_value("next_version") == up.version_num)
                or (up.field_value("next_name") == up.version_name)
            )
            if stale_known:
                for f in ("next_name", "next_version", "next_characters", "preview_time"):
                    up.fields.pop(f, None)

        # 预告类公告（preview_sources，如终末地研发通讯/B站活动说明/干员演示/前瞻直播预告动态）
        # → next_* 注入主条目。角色类字段多条目累加合并；单值字段自动值优先、known_dates 仅兜底
        # （P2-3：B站前瞻自动提取优先于手填 known_dates，known 值在自动值缺失时才显示）；
        # H1 守卫：自指/过早时间不注入。
        if preview_items and updates:
            main_upd = updates[0]
            main_start = _to_date(main_upd.field_value("update_time")) or date.today()
            main_start_s = main_start.isoformat()
            llm_used = False  # P2-1：每轮每游戏最多 1 次 LLM 兜底（控成本）
            for pit in preview_items:
                claims = extract_preview_claims(pit, cfg, main_start)
                for claim in claims:
                    if claim.field == "next_name" and claim.value == main_upd.version_name:
                        continue
                    if claim.field == "next_version" and main_upd.version_num and claim.value == main_upd.version_num:
                        continue
                    if claim.field == "next_update_time":
                        d = claim.value[:10]
                        if not d or d <= main_start_s or d <= today_s:
                            continue
                    if claim.field == "preview_time":
                        d = claim.value[:10]
                        if not d or d <= main_start_s:
                            continue  # 前瞻不得早于本版本开始（防旧版本前瞻残留）
                    cur = main_upd.fields.get(claim.field)
                    if claim.field in ("next_characters", "next_half_characters"):
                        half_set = {n.strip() for n in main_upd.field_value("next_half_characters").split(",") if n.strip()}
                        names = [n.strip() for n in (cur.value if cur else "").split(",") if n.strip()]
                        if claim.field == "next_characters":
                            names = [n for n in names if n not in half_set]
                        for nm in [n.strip() for n in claim.value.split(",") if n.strip()]:
                            if nm and nm not in names and (claim.field != "next_characters" or nm not in half_set):
                                names.append(nm)
                        main_upd.fields[claim.field] = FieldVerdict(
                            field=claim.field, value=",".join(names), confidence=1.0, sources=["parse"]
                        )
                    elif not cur or cur.sources == ["known"]:
                        # 自动抽取值优先；known_dates 手填值仅兜底（P2-3 裁定）
                        main_upd.fields[claim.field] = FieldVerdict(
                            field=claim.field, value=claim.value, confidence=1.0, sources=["parse"]
                        )
                # P2-1 LLM 兜底：触发 = 预告池条目 + 正则未抽到 next_name/next_update_time + 正文含日期线索
                if not llm_used and self.llm_fallback is not None:
                    need_llm = not any(c.field in ("next_name", "next_update_time") for c in claims)
                    pit_content = "\n".join(c.value for c in pit.get("claims", []) if c.field == "content")
                    if need_llm and re.search(r"\d{1,2}月\d{1,2}日|\d{4}/\d{2}/\d{2}", pit_content):
                        llm_used = True
                        try:
                            data = await self.llm_fallback(pit.get("raw_title", ""), pit_content)
                        except Exception as e:
                            self._log(f"[{cfg.display}] LLM 兜底调用异常，静默降级: {e}")
                            data = None
                        self._inject_llm_fields(main_upd, data, cfg, main_start)

            # 后处理：上半名单剔除已归入下半池的角色。
            # known_dates 兑底值走聚合通道（高权重）不经过上方注入过滤，
            # 可能与自动提取的 next_half_characters 重叠（如同角色同时出现在新角色与下半池）。
            # 剔除后可能为空（B站风控时只剩 known 兜底且恰属下半池）→ 同样清空，避免"新角色：伊冯；下半池：伊冯"重复
            _half_v = main_upd.field_value("next_half_characters")
            _up_field = main_upd.fields.get("next_characters")
            if _half_v and _up_field and _up_field.value:
                _half_set = {n.strip() for n in _half_v.split(",") if n.strip()}
                _names = [n.strip() for n in _up_field.value.split(",") if n.strip()]
                _filtered = [n for n in _names if n not in _half_set]
                if _filtered != _names:
                    main_upd.fields["next_characters"] = FieldVerdict(
                        field="next_characters", value=",".join(_filtered),
                        confidence=_up_field.confidence, sources=_up_field.sources,
                    )

        if cfg.activity_mode:
            # ===== P1-1 方舟事件列表状态机（proposal 第四节）=====
            # Event = {name, kind: main|reprint|banner|roadmap, start, end, rough}
            # 状态：active = start ≤ today ≤ end；upcoming = today < start；ended = today > end
            # 主事件 = 进行中（main/reprint）中 start 最新；无则最近 upcoming；再无可显示 → 空窗占位
            # roadmap（制作组通讯）节点带粗窗口字符串（"9月上旬"），不假装精确日期
            events: list[dict] = []

            # 1) 公告事件：来自候选 updates（制作组通讯走 roadmap，不入公告事件）
            for u in updates:
                rt = u.raw_title
                if "制作组通讯" in rt:
                    continue
                if any(k in rt for k in ("时装", "皮肤", "周边", "模组")):
                    continue
                if "复刻" in rt:
                    kind = "reprint"
                elif re.search(r"寻访|甄选", rt):
                    kind = "banner"
                else:
                    kind = "main"
                if kind == "banner":
                    s = _to_date(u.field_value("banner_start")) or _to_date(u.field_value("update_time"))
                    e = _to_date(u.field_value("banner_end"))
                    ev_name = u.field_value("banner_name") or u.version_name
                else:
                    s = _to_date(u.field_value("update_time"))
                    e = _to_date(u.field_value("activity_end"))
                    ev_name = u.version_name
                events.append({
                    "name": ev_name, "kind": kind,
                    "start": s.isoformat() if s else "",
                    "end": e.isoformat() if e else "",
                })

            # 2) roadmap 节点：制作组通讯正文 "SideStory「月行水上」限时活动将于9月上旬开启"
            for it in candidates:
                if "制作组通讯" not in it.get("raw_title", ""):
                    continue
                for c in extract_roadmap_nodes(it):
                    name, window = c.value.split("|", 1)
                    events.append({"name": name, "kind": "roadmap", "start": "", "end": "", "rough": window})

            # 3) 主事件选择
            main_events = [ev for ev in events if ev["kind"] in ("main", "reprint")]
            actives = [ev for ev in main_events if ev["start"] and ev["end"] and ev["start"] <= today_s <= ev["end"]]
            if actives:
                main_ev = max(actives, key=lambda ev: ev["start"])
            else:
                upcomings = [ev for ev in main_events if ev["start"] and ev["start"] > today_s]
                main_ev = min(upcomings, key=lambda ev: ev["start"]) if upcomings else None

            # 4) 主条目输出：主事件对应 update；无主事件 → 空窗占位条目（仍输出卡片，显示"暂无活动"）
            if main_ev is not None:
                main = next(
                    (u for u in updates if u.version_name == main_ev["name"] and "制作组通讯" not in u.raw_title),
                    None,
                )
            else:
                main = None
            if main is None:
                main = GameUpdate(
                    game=cfg.key, game_display=cfg.display, version_num=None,
                    version_name="暂无进行中活动",
                    fields={},
                    raw_urls=[], raw_title="",
                    collected_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                )

            # 5) 事件列表落字段（timeline 渲染驱动）
            main.fields["events"] = FieldVerdict(
                field="events", value=json.dumps(events, ensure_ascii=False),
                confidence=1.0, sources=["parse"],
            )
            if main_ev is not None:
                main.fields["event_kind"] = FieldVerdict(
                    field="event_kind", value=main_ev["kind"], confidence=1.0, sources=["parse"]
                )
                # 复刻标记（驱动栏位A标签与干员前缀）
                if main_ev["kind"] == "reprint":
                    main.fields["is_reprint"] = FieldVerdict(field="is_reprint", value="1", confidence=1.0, sources=["parse"])
                # 当期寻访池信息：banner 事件（池名必有；时间可有可无，图片正文公告可能只有池名）
                banner_evs = [ev for ev in events if ev["kind"] == "banner"]
                if banner_evs and not main.field_value("banner_name"):
                    b = banner_evs[0]
                    main.fields["banner_name"] = FieldVerdict(
                        field="banner_name", value=b["name"], confidence=1.0, sources=["parse"]
                    )
                    if b["start"]:
                        main.fields["banner_start"] = FieldVerdict(
                            field="banner_start", value=b["start"], confidence=1.0, sources=["parse"]
                        )
                    if b["end"]:
                        main.fields["banner_end"] = FieldVerdict(
                            field="banner_end", value=b["end"], confidence=1.0, sources=["parse"]
                        )
            updates = [main]
        return updates

    def _inject_llm_fields(self, main_upd: GameUpdate, data: dict | None, cfg: GameConfig, main_start: date) -> None:
        """P2-1 LLM 兜底注入（带断言护栏，confidence ≤ 0.8 防弱源覆盖官方源）。

        护栏（任一不满足即丢弃对应字段）：
        - next_update_time：必须在未来 60 天内，且与周期推算（main_start + cycle_days）偏差 ≤ 14 天
        - next_name：非空、不等于当前版本名（H1）
        - next_characters：角色名长度 2~12
        """
        if not data or not isinstance(data, dict):
            return
        today = date.today()
        # next_update_time
        nvt = str(data.get("next_update_time") or "").strip()
        if nvt:
            d = _to_date(nvt)
            est = main_start + timedelta(days=cfg.cycle_days)
            if d and today < d <= today + timedelta(days=60) and abs((d - est).days) <= 14:
                cur = main_upd.fields.get("next_update_time")
                if not cur or cur.sources == ["known"]:
                    main_upd.fields["next_update_time"] = FieldVerdict(
                        field="next_update_time", value=nvt[:10], confidence=0.8, sources=["llm"]
                    )
        # next_name
        nn = str(data.get("next_name") or "").strip()
        if nn and nn != main_upd.version_name and not main_upd.field_value("next_name"):
            main_upd.fields["next_name"] = FieldVerdict(
                field="next_name", value=nn, confidence=0.8, sources=["llm"]
            )
        # next_characters（合并去重进上半名单）
        nc = data.get("next_characters")
        if isinstance(nc, list):
            names = [str(x).strip() for x in nc if isinstance(x, str) and 1 < len(x.strip()) <= 12]
            if names:
                cur = main_upd.fields.get("next_characters")
                merged = [n for n in (cur.value.split(",") if cur else []) if n]
                for nm in names:
                    if nm not in merged:
                        merged.append(nm)
                if merged:
                    main_upd.fields["next_characters"] = FieldVerdict(
                        field="next_characters", value=",".join(merged), confidence=0.8, sources=["llm"]
                    )

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
