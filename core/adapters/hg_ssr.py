"""鹰角系 SSR adapter：终末地（官网是服务端渲染，直接抓 HTML）。

列表页: https://endfield.hypergryph.com/news   （SSR，含标题/日期/分类）
详情页: https://endfield.hypergryph.com/news/{id}（如需正文再抓，当前版本列表信息够用）
"""

from __future__ import annotations

import re
from typing import Any

from core.adapters.base import BaseAdapter, fetch_text
from core.models import FieldClaim


class HgSsrAdapter(BaseAdapter):
    WEIGHT = 0.9   # 官网新闻页属 A 级源
    SOURCE_ID = "hg_ssr"

    async def collect(self) -> list[dict[str, Any]]:
        p = self.params
        html = await fetch_text(p["news_url"], p.get("timeout", 15.0))

        # 每条新闻是一个 <a href="/news/xxx"> 链接（可能带 query），标题在链接文本或 img alt 里
        pattern = re.compile(
            r'<a[^>]*href="([^"]*?/news/\d+[^"]*)"[^>]*>(.*?)</a>', re.S | re.I
        )
        items: list[dict[str, Any]] = []
        for m in pattern.finditer(html):
            href, inner = m.group(1), m.group(2)
            # 标题：优先 a 内纯文本，其次 img alt
            title = re.sub(r"<[^>]+>", "", inner)
            title = re.sub(r"\s+", "", title).strip()
            if not title:
                alt = re.search(r'<img[^>]*alt="([^"]+)"', inner)
                title = re.sub(r"\s+", "", alt.group(1)).strip() if alt else ""
            if not title or len(title) > 60:
                continue

            # 日期：a 块内找 2026.07.16 或 2026-07-16 格式（公告发布日期≈版本更新日）
            update_time = ""
            dm = re.search(r"(20\d{2})\.(\d{2})\.(\d{2})", inner)
            if dm:
                update_time = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
            else:
                dm2 = re.search(r"(20\d{2})-(\d{2})-(\d{2})", inner)
                if dm2:
                    update_time = f"{dm2.group(1)}-{dm2.group(2)}-{dm2.group(3)}"

            if href.startswith("http"):
                url = href
            elif href.startswith("/"):
                url = "https://endfield.hypergryph.com" + href
            else:
                url = "https://endfield.hypergryph.com/news/" + href

            claims = [
                FieldClaim(field="raw_title", value=title, source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
            ]
            if update_time:
                claims.append(FieldClaim(field="update_time", value=update_time, source=self.SOURCE_ID, weight=self.WEIGHT, url=url))
            items.append({"raw_title": title, "claims": claims, "url": url, "raw": {"href": href, "title": title, "date": update_time}})

        # 去重（同一标题可能出现多次）
        seen = set()
        uniq: list[dict[str, Any]] = []
        for it in items:
            if it["raw_title"] not in seen:
                seen.add(it["raw_title"])
                uniq.append(it)
        return uniq
