"""米哈游系 adapter：崩铁 / 绝区零 复用。

数据形态：getAnnList / getAnnContent 双接口，结构化 JSON。
崩铁:  https://hkrpg-api-static.mihoyo.com/common/hkrpg_cn/announcement/api/getAnnList
绝区零: https://announcement-api.mihoyo.com/common/nap_cn/announcement/api/getAnnList
"""

from __future__ import annotations

import re
from typing import Any

from guw_core.adapters.base import BaseAdapter, fetch_json
from guw_core.models import FieldClaim


class MihoyoJsonAdapter(BaseAdapter):
    WEIGHT = 1.0
    SOURCE_ID = "mihoyo_json"

    def _ann_list_url(self) -> str:
        p = self.params
        return (
            f"{p['api_base']}/common/{p['game_biz']}/announcement/api/getAnnList"
            f"?game={p['game']}&game_biz={p['game_biz']}&lang=zh-cn"
            f"&bundle_id={p.get('bundle_id', p['game_biz'])}&platform=pc"
            f"&region={p.get('region', 'prod_gf_cn')}&level={p.get('level', 30)}&uid={p.get('uid', '11111111')}"
            + (f"&channel_id={p['channel_id']}" if p.get("channel_id") else "")
        )

    def _ann_content_url(self) -> str:
        p = self.params
        return (
            f"{p['api_base']}/common/{p['game_biz']}/announcement/api/getAnnContent"
            f"?game={p['game']}&game_biz={p['game_biz']}&lang=zh-cn"
            f"&bundle_id={p.get('bundle_id', p['game_biz'])}&platform=pc"
            f"&region={p.get('region', 'prod_gf_cn')}&level={p.get('level', 30)}&uid={p.get('uid', '11111111')}"
        )

    async def collect(self) -> list[dict[str, Any]]:
        p = self.params
        list_data = await fetch_json(self._ann_list_url(), p.get("timeout", 15.0))
        if list_data.get("retcode") != 0:
            raise RuntimeError(f"米哈游 getAnnList retcode={list_data.get('retcode')} msg={list_data.get('message')}")

        # 正文一次性拉全（getAnnContent 返回全部公告正文，按 ann_id 映射）
        contents: dict[str, str] = {}
        try:
            content_data = await fetch_json(self._ann_content_url(), p.get("timeout", 15.0))
            if content_data.get("retcode") == 0:
                for item in content_data["data"]["list"]:
                    contents[str(item["ann_id"])] = item.get("content", "")
        except Exception:
            pass  # 正文拉取失败不影响列表

        # 网页版公告地址（供用户点击）。优先用游戏配置的 web_base（官网新闻页）
        web_base = p.get("web_base", "")
        if web_base and not web_base.endswith(('?', '&')):
            web_base = web_base.rstrip("/")

        items: list[dict[str, Any]] = []
        for group in list_data["data"]["list"]:
            for item in group.get("list", []):
                title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                ann_id = str(item.get("ann_id"))
                url = f"{web_base}?ann_id={ann_id}" if web_base else f"https://webstatic.mihoyo.com/{p['game']}/announcement/index.html?game={p['game']}&game_biz={p['game_biz']}&lang=zh-cn&bundle_id={p.get('bundle_id', p['game_biz'])}&platform=pc"
                claims = [
                    FieldClaim(field="raw_title", value=title, source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
                    FieldClaim(field="start_time", value=item.get("start_time", ""), source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
                    FieldClaim(field="end_time", value=item.get("end_time", ""), source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
                    FieldClaim(field="tag_label", value=item.get("tag_label", ""), source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
                    FieldClaim(field="ann_id", value=ann_id, source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
                    # 版本更新说明类公告的 start_time 即版本更新时间，兼作节奏锚点
                    FieldClaim(field="update_time", value=item.get("start_time", ""), source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
                ]
                content = contents.get(ann_id, "")
                if content:
                    claims.append(FieldClaim(field="content", value=content, source=self.SOURCE_ID, weight=self.WEIGHT, url=url))
                items.append({"raw_title": title, "claims": claims, "url": url, "raw": item})
        return items
