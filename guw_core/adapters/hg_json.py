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
        for item in data["data"]["list"]:
            cid = str(item["cid"])
            detail_url = f"{p['api_base']}/api/game/bulletin/{cid}"
            # 方舟 API 标题里的 \n 是字面反斜杠+n（非真实换行）
            title = item.get("title", "").strip().replace("\\n", "")
            claims = [
                FieldClaim(field="raw_title", value=title, source=self.SOURCE_ID, weight=self.WEIGHT, url=detail_url),
                FieldClaim(field="display_time", value=item.get("displayTime", ""), source=self.SOURCE_ID, weight=self.WEIGHT, url=detail_url),
                FieldClaim(field="category", value=str(item.get("category", "")), source=self.SOURCE_ID, weight=self.WEIGHT, url=detail_url),
            ]
            # 拉详情补充正文
            try:
                det = await fetch_json(detail_url, p.get("timeout", 15.0))
                if det.get("code") == 0 and det.get("data", {}).get("content"):
                    claims.append(FieldClaim(field="content", value=det["data"]["content"], source=self.SOURCE_ID, weight=self.WEIGHT, url=detail_url))
            except Exception:
                pass
            items.append({"raw_title": title, "claims": claims, "url": detail_url, "raw": item})
        return items
