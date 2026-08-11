"""game-update-watcher：MaiBot 插件。

采集多款游戏（鸣潮/方舟/终末地/绝区零/崩铁）的版本更新公告，
字段级交叉认证后按 6 周版本节奏生成汇总图。

触发方式（按优先级）：
1. Tool「游戏更新速报」：LLM 在对话中判断用户需要时自动调用，发图到当前聊天流
2. Command「/游戏速报」：手动触发，发图到当前聊天流
3. 定时轮询（可选）：config 里 scheduled_enabled=true 时启用，发到 default_groups

发送链路：ctx.send.image(base64, stream_id)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from core.pipeline import UpdatePipeline
from core.renderer import render_summary
from core.store import PublishStore
from core.timeline import build_timeline


class GameUpdatePlugin(MaiBotPlugin):
    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._task: asyncio.Task | None = None
        # 依赖检查：httpx / pillow 是第三方库，MaiBot 不保证自带
        missing = []
        for mod in ("httpx", "PIL"):
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        if missing:
            self.ctx.logger.error(
                "game-update-watcher 缺少依赖: %s，请执行 pip install httpx pillow 后重启",
                ", ".join(missing),
            )
            return
        data_dir = self.ctx.paths.data_dir
        runtime_dir = self.ctx.paths.runtime_dir

        # 读取插件配置（config.toml → SDK config.get_all）
        self._cfg = await self.ctx.config.get_all() or {}
        # 兼容两种结构：{"plugin": {...}} 或平铺
        self._cfg_plugin = self._cfg.get("plugin", {}) if isinstance(self._cfg.get("plugin"), dict) else self._cfg

        self._pipeline = UpdatePipeline(logger=self.ctx.logger)
        games_dir = Path(__file__).parent / "games"
        self._games = self._pipeline.load_games(games_dir)
        self._store = PublishStore(data_dir / "published.db")
        self._pipeline.store = self._store
        self._runtime_dir = runtime_dir

        self.ctx.logger.info(
            "game-update-watcher 加载完成，监控游戏: %s",
            ", ".join(c.display for c in self._games.values()),
        )

        # 定时轮询（可选）：默认关闭，用 Tool/Command 按需触发
        if self._cfg_plugin.get("scheduled_enabled", False):
            poll_min = int(self._cfg_plugin.get("poll_interval_minutes", 360) or 360)
            self._task = asyncio.create_task(self._poll_loop(poll_min))
            self.ctx.logger.info("game-update-watcher 定时轮询已启用，间隔 %s 分钟", poll_min)
        else:
            self.ctx.logger.info("game-update-watcher 定时轮询未启用，使用 Tool/命令按需触发")

    async def on_unload(self) -> None:
        if self._task:
            self._task.cancel()
        if hasattr(self, "_store"):
            self._store.close()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        # 配置热更新时刷新本地副本，下一次调用即生效
        if scope == "self" and isinstance(config_data, dict):
            self._cfg = config_data
            self._cfg_plugin = config_data.get("plugin", {}) if isinstance(config_data.get("plugin"), dict) else config_data
            self.ctx.logger.info("game-update-watcher 配置已热更新")

    # ---------- 触发入口 ----------

    @Tool(
        "game_update_report",
        brief_description="生成并发送「游戏更新速报」汇总图（包含鸣潮/明日方舟/绝区零/崩铁等游戏的版本节奏、卡池、前瞻时间）",
        detailed_description=(
            "当用户询问游戏版本更新、卡池时间、前瞻直播、新角色等消息时调用。"
            "会采集各游戏官方公告，生成一张汇总图片并发送到当前聊天流。\n"
            "参数说明：\n"
            "- stream_id：string，必填。当前聊天流 ID，由系统注入。"
        ),
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=True,
            ),
        ],
    )
    async def handle_tool_report(self, stream_id: str, **kwargs) -> dict:
        del kwargs
        result = await self._build_and_send(stream_id)
        return {"success": result[0], "message": result[1]}

    @Command("游戏速报", pattern=r"^/游戏速报")
    async def handle_cmd_report(self, **kwargs) -> tuple[bool, str, int]:
        stream_id = kwargs.get("stream_id", "")
        if not stream_id:
            return False, "无法获取当前聊天流", 2
        ok, msg = await self._build_and_send(stream_id)
        return ok, msg, 2

    # ---------- 核心：采集 + 渲染 + 发送到指定聊天流 ----------

    async def _build_and_send(self, stream_id: str) -> tuple[bool, str]:
        """采集所有游戏 → 渲染汇总图 → 发送到指定聊天流。返回 (是否成功, 说明)。"""
        cfg_plugin = self._cfg_plugin
        threshold = float(cfg_plugin.get("publish_threshold", 0.8))
        timeout = float(cfg_plugin.get("http_timeout_seconds", 15))
        watermark = cfg_plugin.get("debug", False) and "DEBUG" or ""

        entries: list[tuple] = []  # (update, cfg, timeline)
        errors: list[str] = []
        for key, gc in self._games.items():
            try:
                candidates = await self._pipeline.collect_game(gc, timeout)
                updates = await self._pipeline.build_updates(gc, candidates, threshold)
                for up in updates:
                    tl = build_timeline(up, gc)
                    entries.append((up, gc, tl))
            except Exception as e:
                self.ctx.logger.exception("[%s] 处理失败: %s", gc.display, e)
                errors.append(f"{gc.display}: {e}")

        if not entries:
            return False, "采集失败或没有可用数据" + (f"（{len(errors)} 个游戏出错）" if errors else "")

        try:
            png = render_summary(entries, self._runtime_dir / "summary.png", watermark)
            b64 = self._pipeline.encode_image(png)
            ok = await self.ctx.send.image(image_data=b64, stream_id=stream_id)
            if ok:
                msg = f"已生成游戏更新速报（{len(entries)} 条）"
                if errors:
                    msg += f"，{len(errors)} 个游戏采集失败"
                return True, msg
            return False, "图片发送失败"
        except Exception as e:
            self.ctx.logger.exception("汇总图渲染/发送失败: %s", e)
            return False, f"生成失败: {e}"

    # ---------- 定时轮询（可选） ----------

    async def _poll_loop(self, interval_minutes: int) -> None:
        await self._run_once()
        while True:
            try:
                await asyncio.sleep(interval_minutes * 60)
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ctx.logger.exception("game-update-watcher 轮询异常: %s", e)

    async def _run_once(self) -> None:
        cfg_plugin = self._cfg_plugin
        if not cfg_plugin.get("enabled", True):
            return
        threshold = float(cfg_plugin.get("publish_threshold", 0.8))
        timeout = float(cfg_plugin.get("http_timeout_seconds", 15))
        default_groups: list[str] = cfg_plugin.get("default_groups", [])
        watermark = cfg_plugin.get("debug", False) and "DEBUG" or ""

        # 只发新条目（去重）
        new_entries: list[tuple] = []
        for key, gc in self._games.items():
            try:
                candidates = await self._pipeline.collect_game(gc, timeout)
                updates = await self._pipeline.build_updates(gc, candidates, threshold)
                for up in updates:
                    if not self._pipeline.is_new(up):
                        continue
                    tl = build_timeline(up, gc)
                    new_entries.append((up, gc, tl))
            except Exception as e:
                self.ctx.logger.exception("[%s] 处理失败: %s", gc.display, e)

        if not new_entries:
            return

        groups = default_groups
        if not groups:
            self.ctx.logger.warning("有 %s 条新更新但未配置 default_groups，跳过发送", len(new_entries))
            return

        try:
            png = render_summary(new_entries, self._runtime_dir / "summary.png", watermark)
            b64 = self._pipeline.encode_image(png)
            sent = await self._send_image_to_groups(b64, groups)
            if sent:
                for up, gc, _ in new_entries:
                    self._pipeline.mark_sent(up)
                self.ctx.logger.info("定时推送汇总图（%s 条更新）→ %s 个群", len(new_entries), len(groups))
        except Exception as e:
            self.ctx.logger.exception("定时推送失败: %s", e)

    async def _send_image_to_groups(self, image_b64: str, groups: list[str]) -> bool:
        """多群发送：单个群失败独立重试，不阻塞其他群。"""
        sent_any = False
        for gid in groups:
            try:
                session = await self.ctx.chat.open_session(
                    platform="qq", chat_type="group", group_id=str(gid),
                )
                stream_id = session.get("stream_id") if isinstance(session, dict) else session
                if not stream_id:
                    self.ctx.logger.error("无法为群 %s 打开聊天流: %s", gid, session)
                    continue
                ok = await self.ctx.send.image(image_data=image_b64, stream_id=stream_id)
                if ok:
                    sent_any = True
                    self.ctx.logger.info("已发送到群 %s", gid)
                else:
                    self.ctx.logger.warning("群 %s 发送返回 False", gid)
            except Exception as e:
                self.ctx.logger.exception("发送到群 %s 失败: %s", gid, e)
        return sent_any


def create_plugin():
    return GameUpdatePlugin()
