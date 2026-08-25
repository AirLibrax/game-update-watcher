"""Adapter 注册表：按配置的 adapter 名实例化。"""

from __future__ import annotations

from guw_core.adapters.base import BaseAdapter
from guw_core.adapters.bili_dynamic import BiliDynamicAdapter
from guw_core.adapters.hg_json import HgJsonAdapter
from guw_core.adapters.hg_ssr import HgSsrAdapter
from guw_core.adapters.kuro import KuroJsonAdapter
from guw_core.adapters.mihoyo import MihoyoJsonAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {
    "mihoyo_json": MihoyoJsonAdapter,
    "hg_json": HgJsonAdapter,
    "hg_ssr": HgSsrAdapter,
    "kuro_json": KuroJsonAdapter,
    "bili_dynamic": BiliDynamicAdapter,
}


def create_adapter(adapter_name: str, params: dict, logger=None) -> BaseAdapter:
    if adapter_name not in ADAPTERS:
        raise ValueError(f"未知 adapter: {adapter_name}，可用: {list(ADAPTERS)}")
    return ADAPTERS[adapter_name](params, logger=logger)
