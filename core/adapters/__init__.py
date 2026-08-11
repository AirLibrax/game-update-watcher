"""Adapter 注册表：按配置的 adapter 名实例化。"""

from __future__ import annotations

from core.adapters.base import BaseAdapter
from core.adapters.hg_json import HgJsonAdapter
from core.adapters.hg_ssr import HgSsrAdapter
from core.adapters.kuro import KuroJsonAdapter
from core.adapters.mihoyo import MihoyoJsonAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {
    "mihoyo_json": MihoyoJsonAdapter,
    "hg_json": HgJsonAdapter,
    "hg_ssr": HgSsrAdapter,
    "kuro_json": KuroJsonAdapter,
}


def create_adapter(adapter_name: str, params: dict, logger=None) -> BaseAdapter:
    if adapter_name not in ADAPTERS:
        raise ValueError(f"未知 adapter: {adapter_name}，可用: {list(ADAPTERS)}")
    return ADAPTERS[adapter_name](params, logger=logger)
