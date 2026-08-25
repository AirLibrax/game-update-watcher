"""鹰角系 JSON adapter：明日方舟。

数据形态：bulletinList 列表 + bulletin/{cid} 详情，结构化 JSON。
列表:  https://ak-webview.hypergryph.com/api/game/bulletinList?target=IOS
详情:  https://ak-webview.hypergryph.com/api/game/bulletin/{cid}   （路径参数）
"""

from __future__ import annotations

from typing import Any

from guw_core.adapters.base import BaseAdapter, fetch_json
from guw_core.models import FieldClaim


class HgJsonAdapter(BaseAdapter):
    WEIGHT = 1.0
    SOURCE_ID = "hg_json"

    async def collect(self) -> list[dict[str, Any]]:
        p = self.params
        list_url = f"{p['api_base']}/api/game/bulletinList?target={p.get('target', 'IOS')}"
        data = await fetch_json(list_url, p.get("timeout", 15.0))
        if data.get("code") != 0:
            raise RuntimeError(f"方舟 bulletinList code={data.get('code')} msg={data.get('msg')}")

        items: list[dict[str, Any]] = []
        # H2 修复：只对最新 detail_limit 条（或标题含活动/寻访关键词者）拉详情，
        # 避免每轮对全量 24 条发详情请求（旧实现 25 请求/轮）。
        # 关键词强制兜底：核心公告（复刻预告/制作组通讯/活动开启）即使超出 limit 也必拉详情
        detail_limit = int(p.get("detail_limit", 5))
        for i, item in enumerate(data["data"]["list"]):
            cid = str(item["cid"])
            detail_url = f"{p['api_base']}/api/game/bulletin/{cid}"
            # 方舟 API 标题里的 \n 是字面反斜杠+n（非真实换行）
            title = item.get("title", "").strip().replace("\\n", "")
            claims = [
                FieldClaim(field="raw_title", value=title, source=self.SOURCE_ID, weight=self.WEIGHT, url=detail_url),
                FieldClaim(field="display_time", value=item.get("displayTime", ""), source=self.SOURCE_ID, weight=self.WEIGHT, url=detail_url),
                FieldClaim(field="category", value=str(item.get("category", "")), source=self.SOURCE_ID, weight=self.WEIGHT, url=detail_url),
            ]
            # 详情仅在必要时拉取：最新 N 条，或标题含活动/寻访/通讯关键词（如复刻预告、制作组通讯）
            need_detail = i < detail_limit or any(
                k in title for k in ("开启", "预告", "通讯", "寻访", "活动")
            )
            if need_detail:
                try:
                    det = await fetch_json(detail_url, p.get("timeout", 15.0))
                    if det.get("code") == 0 and det.get("data", {}).get("content"):
                        claims.append(FieldClaim(field="content", value=det["data"]["content"], source=self.SOURCE_ID, weight=self.WEIGHT, url=detail_url))
                except Exception:
                    pass
            items.append({"raw_title": title, "claims": claims, "url": detail_url, "raw": item})
        return items
