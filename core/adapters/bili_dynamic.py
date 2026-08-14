"""B站官方账号动态 adapter：作为第二信息源，用于字段级交叉认证。

数据形态：B站动态 API（web 端），需要 wbi 签名。
  1. 从 nav 接口匿名获取 wbi 密钥（img_key/sub_key）
  2. 对请求参数做 wbi 签名（mixinKey + md5）
  3. 请求用户动态列表，从动态文本/转发内容提取版本信息

用途：官方账号的前瞻预告、版本更新动态可交叉验证主源抓到的
     preview_time / update_time / next_update_time 等字段。
权重 0.9（A 级：官方内容出口）。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import re
import time
import urllib.parse
from typing import Any

import httpx

from core.adapters.base import BaseAdapter
from core.models import FieldClaim

# wbi mixin key 打乱表（B站前端固定算法，公开）
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"

# 结果缓存：官方动态变化不频繁，缓存可减少请求次数、避免触发 B站风控
# 模块级全局状态：同一进程内所有实例共享，节省重复请求
_CACHE_TTL_SECONDS = 900  # 15 分钟
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

# 全局节流：不同 uid 的连续请求也拉开间隔（B站对短时间多请求有风控）
_MIN_REQUEST_GAP = 1.5  # 秒
_last_request_time: float = 0.0


def _get_mixin_key(orig: str) -> str:
    """按打乱表取出 32 位 mixin key。"""
    return "".join(orig[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _enc_wbi(params: dict[str, str], img_key: str, sub_key: str) -> dict[str, str]:
    """对参数做 wbi 签名，返回带 w_rid/wts 的完整参数。"""
    mixin_key = _get_mixin_key(img_key + sub_key)
    params["wts"] = str(int(time.time()))
    # 过滤 value 为空或含 !'()* 的键，然后按 key 排序
    filtered = {k: v for k, v in params.items() if v and re.search(r"[!'()*]", v) is None}
    query = urllib.parse.urlencode(sorted(filtered.items()))
    params["w_rid"] = _md5(query + mixin_key)
    return params


class BiliDynamicAdapter(BaseAdapter):
    """B站官方账号动态源。params 需含 uid（B站用户 ID）。"""

    WEIGHT = 0.9
    SOURCE_ID = "bili_dynamic"

    async def _session_get_json(self, client, url: str) -> Any:
        """用共享 session 请求，保持 cookie（B站动态接口需要 buvid3 等）。"""
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def _get_wbi_keys(self, client) -> tuple[str, str]:
        data = await self._session_get_json(client, _NAV_URL)
        wbi = data.get("data", {}).get("wbi_img", {})
        img_url = wbi.get("img_url", "")
        sub_url = wbi.get("sub_url", "")
        img_key = img_url.split("/")[-1].split(".")[0]
        sub_key = sub_url.split("/")[-1].split(".")[0]
        if not img_key or not sub_key:
            raise RuntimeError("B站 nav 接口未返回 wbi 密钥")
        return img_key, sub_key

    async def collect(self) -> list[dict[str, Any]]:
        p = self.params
        uid = str(p["uid"])

        # 缓存命中：同一 uid 在 TTL 内直接返回上次结果，避免连发请求触发 B站风控
        now = time.time()
        cached = _cache.get(uid)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            self._log(f"B站动态缓存命中 uid={uid}，跳过网络请求")
            return cached[1]

        # 全局节流：距上次真实请求不足间隔则等待，避免不同 uid 连发触发风控
        global _last_request_time
        wait = _MIN_REQUEST_GAP - (now - _last_request_time)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_time = time.time()

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }
        # 同一个 session：先访问主页拿 buvid cookie，再拿 wbi 密钥，最后请求动态
        async with httpx.AsyncClient(timeout=p.get("timeout", 15.0), headers=headers, follow_redirects=True) as client:
            # 访问主页触发 Set-Cookie（buvid3 等）
            try:
                await client.get("https://www.bilibili.com/")
            except Exception:
                pass  # 主页失败不影响，cookie 可能已种下

            img_key, sub_key = await self._get_wbi_keys(client)

            params = _enc_wbi(
                {"host_mid": uid, "timezone_offset": "-480", "features": "itemOpusStyle"},
                img_key, sub_key,
            )
            url = f"{_DYNAMIC_URL}?{urllib.parse.urlencode(params)}"
            data = await self._session_get_json(client, url)
            if not isinstance(data, dict):
                raise RuntimeError(f"B站动态接口返回异常: {str(data)[:200]}")

        if data.get("code") != 0:
            raise RuntimeError(f"B站动态接口 code={data.get('code')} msg={data.get('message')}")

        payload = data.get("data") or {}
        raw_items = payload.get("items") or []
        if not raw_items:
            # B站对匿名请求有间隔风控，可能返回空列表；这是正常现象，下次轮询会恢复
            self._log("B站动态返回空列表（可能命中风控冷却），本次跳过")

        items: list[dict[str, Any]] = []
        for card in raw_items:
            # 跳过置顶动态（防假冒声明、公告置顶等，不属于版本信息）
            tag = (card.get("modules", {}).get("module_tag") or {}).get("text", "")
            if tag == "置顶":
                continue
            dyn = card.get("modules", {}).get("module_dynamic", {}) or {}
            text = self._extract_text(card)
            if not text:
                continue
            url = f"https://www.bilibili.com/opus/{card.get('id_str', '')}" if card.get("id_str") else ""
            claims = [
                FieldClaim(field="raw_title", value=text[:60], source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
                FieldClaim(field="content", value=text, source=self.SOURCE_ID, weight=self.WEIGHT, url=url),
            ]
            ts = dyn.get("timestamp")
            if ts:
                dt = datetime.datetime.fromtimestamp(ts)
                claims.append(FieldClaim(field="pub_time", value=dt.strftime("%Y-%m-%d %H:%M"), source=self.SOURCE_ID, weight=self.WEIGHT, url=url))
            items.append({"raw_title": text[:60], "claims": claims, "url": url, "raw": card})
        # 只缓存非空结果：空列表（风控）不缓存，下次触发立即重试
        if items:
            _cache[uid] = (time.time(), items)
        return items

    def _extract_text(self, card: dict) -> str:
        """从动态卡片提取文本，兼容多种动态类型。

        结构路径（新版 web 动态接口）：
        - 图文/文本: modules.module_dynamic.major.opus.summary.text
        - 图文旧版:  modules.module_dynamic.major.draw
        - 纯文本:    modules.module_dynamic.desc.text
        - 转发:      转发卡片里再嵌套 orig
        递归兜底：遍历所有 dict 找 text 字段。
        """
        dyn = card.get("modules", {}).get("module_dynamic", {}) or {}

        # 1. major 各类
        major = dyn.get("major") or {}
        for key in ("opus", "major_text"):
            sub = major.get(key) or {}
            summary = (sub.get("summary") or {}).get("text", "")
            if summary:
                return summary
            # opus 还可能直接带 summary_text
            if sub.get("summary_text"):
                return sub["summary_text"]
        # 1b. 视频/专栏动态：标题在 archive 里
        for key in ("archive", "article"):
            sub = major.get(key) or {}
            title = sub.get("title", "")
            if title:
                return title
        # 2. desc 纯文本
        desc_text = (dyn.get("desc") or {}).get("text", "")
        if desc_text:
            return desc_text
        # 3. 转发：取被转发内容（嵌套 orig 结构）
        orig = card.get("orig") or {}
        if orig:
            t = self._extract_text(orig)
            if t:
                return t
        # 4. 递归兜底：深度遍历找 text 字段
        return self._find_text_deep(card)

    def _find_text_deep(self, obj, depth: int = 0) -> str:
        """深度优先遍历，找第一个非空 text/description 字段（兜底）。"""
        if depth > 6:
            return ""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("text", "description", "summary") and isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, (dict, list)):
                    t = self._find_text_deep(v, depth + 1)
                    if t:
                        return t
        elif isinstance(obj, list):
            for v in obj:
                t = self._find_text_deep(v, depth + 1)
                if t:
                    return t
        return ""
