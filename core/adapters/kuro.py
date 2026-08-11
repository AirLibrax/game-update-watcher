"""库洛系 adapter：鸣潮。

数据形态：单 URL 全量返回游戏内公告（JSON + HTML 正文）。
URL: https://aki-gm-resources-back.aki-game.com/gamenotice/G152/76402e5b20be2c39f095a152090afddc/zh-Hans.json
"""

from __future__ import annotations

import html as html_mod
import re
from typing import Any

from core.adapters.base import BaseAdapter, fetch_json
from core.models import FieldClaim


def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</?(?:div|p|li|h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html_mod.unescape(s)


class KuroJsonAdapter(BaseAdapter):
    WEIGHT = 1.0
    SOURCE_ID = "kuro_json"

    async def collect(self) -> list[dict[str, Any]]:
        p = self.params
        data = await fetch_json(p["notice_url"], p.get("timeout", 15.0))

        items: list[dict[str, Any]] = []
        for notice in data.get("game", []):
            content_html = notice.get("content", "")
            content_text = strip_html(content_html)

            # 标题提取策略：
            # 1. 优先取含「」和版本关键词的行（如「遗音扶剑，荡梦而歌」3.5版本内容介绍如下）
            # 2. 其次取第一行非客套话的行
            title = ""
            for line in content_text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "「" in line and ("版本" in line or "更新" in line or "内容" in line):
                    title = line
                    break
            if not title:
                for line in content_text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    if any(k in line for k in ("亲爱的", "感谢", "还请", "查阅", "敬请")):
                        continue
                    title = line
                    break
            if not title:
                title = p.get("title_override") or (content_text[:40] + "…")
            # 截断过长的标题行
            if len(title) > 60:
                title = title[:60] + "…"

            url = p.get("notice_url")
            claims = [
                FieldClaim(field="raw_title", value=title, source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
                FieldClaim(field="content", value=content_text, source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
            ]
            for ts_field, claim_field in (
                ("startTimeMs", "start_time_ms"),
                ("endTimeMs", "end_time_ms"),
                ("operateTimeMs", "publish_time_ms"),
            ):
                if notice.get(ts_field):
                    claims.append(FieldClaim(
                        field=claim_field,
                        value=str(notice[ts_field]),
                        source=self.SOURCE_ID, weight=self.WEIGHT, url=url,
                    ))
            items.append({"raw_title": title, "claims": claims, "url": url, "raw": notice})
        return items
