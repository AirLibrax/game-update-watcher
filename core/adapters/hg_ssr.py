"""鹰角系 SSR adapter：终末地（官网是服务端渲染，数据内嵌在 HTML 的 JSON 里）。

列表页: https://endfield.hypergryph.com/news
数据:  页面 HTML 内嵌 JSON（Next.js RSC 数据），含 cid/title/displayTime/brief
详情:  https://endfield.hypergryph.com/news/{cid}

解析策略：HTML 结构（标题+日期）与内嵌 JSON（cid）分别提取，按出现顺序配对。
对最新 N 条抓详情页补正文（角色等字段在详情里）。
"""

from __future__ import annotations

import re
from typing import Any

from core.adapters.base import BaseAdapter, fetch_text
from core.models import FieldClaim


def _strip_html(s: str) -> str:
    """剥掉 HTML 标签，保留可读文本。"""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</?(?:div|p|li|h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return s


class HgSsrAdapter(BaseAdapter):
    WEIGHT = 0.9   # 官网新闻页属 A 级源
    SOURCE_ID = "hg_ssr"

    async def collect(self) -> list[dict[str, Any]]:
        p = self.params
        html = await fetch_text(p["news_url"], p.get("timeout", 15.0))

        # 1. HTML 结构解析：标题 + 日期（顺序与 cid 一致）
        struct_items: list[dict] = []
        for m in re.finditer(
            r'<img[^>]*alt="([^"]+)"[^>]*class="[^"]*NoticeList_image[^"]*"[^>]*/>.*?'
            r'NoticeList_date[^>]*>(\d{4}\.\d{2}\.\d{2})</span>.*?'
            r'NoticeList_title[^>]*>([^<]+)</div>',
            html, re.S,
        ):
            alt, date_str, title = m.group(1), m.group(2), m.group(3)
            del alt  # img alt 与标题一致，仅用于定位
            title = re.sub(r"\s+", " ", title).strip()
            if not title or len(title) > 60:
                continue
            date_parts = date_str.split(".")
            update_time = f"{date_parts[0]}-{date_parts[1]}-{date_parts[2]}" if len(date_parts) == 3 else ""
            struct_items.append({"title": title, "update_time": update_time})

        # 2. 内嵌 JSON 提取 cid（顺序与 HTML 结构一致）
        cids = re.findall(r'\\?"cid\\?":\\?"(\d+)\\?"', html)

        # 3. 配对并抓详情（只抓前 N 条，角色在详情正文里）
        detail_limit = int(p.get("detail_limit", 5))
        items: list[dict[str, Any]] = []
        for i, st in enumerate(struct_items[:detail_limit]):
            cid = cids[i] if i < len(cids) else ""
            url = f"https://endfield.hypergryph.com/news/{cid}" if cid else ""
            claims = [
                FieldClaim(field="raw_title", value=st["title"], source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
            ]
            if st["update_time"]:
                claims.append(FieldClaim(field="update_time", value=st["update_time"], source=self.SOURCE_ID, weight=self.WEIGHT, url=url))
            # 详情页补正文（角色/武器等字段）
            if cid:
                try:
                    detail_html = await fetch_text(url, p.get("timeout", 15.0))
                    content = _strip_html(detail_html)
                    if content:
                        claims.append(FieldClaim(field="content", value=content, source=self.SOURCE_ID, weight=self.WEIGHT, url=url))
                except Exception:
                    pass
            items.append({
                "raw_title": st["title"],
                "claims": claims,
                "url": url,
                "raw": {"cid": cid, "title": st["title"]},
            })

        # 4. 列表页其余条目（不抓详情，保留列表信息）
        for i, st in enumerate(struct_items[detail_limit:]):
            idx = detail_limit + i
            cid = cids[idx] if idx < len(cids) else ""
            url = f"https://endfield.hypergryph.com/news/{cid}" if cid else ""
            claims = [
                FieldClaim(field="raw_title", value=st["title"], source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
            ]
            if st["update_time"]:
                claims.append(FieldClaim(field="update_time", value=st["update_time"], source=self.SOURCE_ID, weight=self.WEIGHT, url=url))
            items.append({
                "raw_title": st["title"],
                "claims": claims,
                "url": url,
                "raw": {"cid": cid, "title": st["title"]},
            })

        return items
