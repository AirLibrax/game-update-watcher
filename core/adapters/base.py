"""Adapter 基类与 HTTP 工具。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

import httpx

from core.models import FieldClaim


class FetchError(Exception):
    pass


async def fetch_json(url: str, timeout: float = 15.0, headers: dict | None = None) -> Any:
    """GET 并解析 JSON。"""
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
    if headers:
        h.update(headers)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=h, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise FetchError(f"GET {url} 失败: {e}") from e


async def fetch_text(url: str, timeout: float = 15.0, headers: dict | None = None) -> str:
    """GET 并返回文本。"""
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
    if headers:
        h.update(headers)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=h, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        raise FetchError(f"GET {url} 失败: {e}") from e


class BaseAdapter(ABC):
    """采集适配器基类。每个 adapter 对应一种「数据形态」，可被多款游戏复用。

    输出：从接口数据中提取原始字段声明（FieldClaim 列表）。
    解析规则（哪个标题是版本更新、怎么抠出版本号）交给 parse 层，不在此处写死。
    """

    def __init__(self, params: dict[str, Any], logger=None):
        self.params = params
        self.logger = logger

    @abstractmethod
    async def collect(self) -> list[dict[str, Any]]:
        """返回原始条目列表，每项是 dict，至少包含:
        - raw_title: 原始标题
        - claims: list[FieldClaim] 已提取的字段声明
        - url: 详情链接
        - raw: 原始数据
        """
        raise NotImplementedError

    def _log(self, msg: str) -> None:
        if self.logger:
            self.logger.info(msg)
