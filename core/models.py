"""游戏更新插件：数据模型定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------- 字段级可信度模型 ----------

# 信息源可信度等级（全局统一，可被游戏配置覆盖）
SOURCE_WEIGHTS: dict[str, float] = {
    "S": 1.0,   # 官方数据接口/官方社区 API
    "A": 0.9,   # 官方内容出口（官网新闻、B站官号动态）
    "B": 0.6,   # 权威第三方（wiki、Gamekee）
    "C": 0.4,   # 泛第三方聚合
    "D": 0.2,   # 社区爆料
}

# 每个字段可收集到的原始声明（来自不同源）
@dataclass
class FieldClaim:
    field: str            # version / preview_time / update_time / characters / link
    value: str
    source: str           # 源标识，如 "kuro_json"
    weight: float         # 该源的权重
    url: str = ""         # 详情链接（用于溯源）

    @property
    def normalized(self) -> str:
        """归一化用于比较：去空白、统一全角/半角数字、小写。"""
        import re
        v = re.sub(r"\s+", "", self.value)
        v = v.replace("：", ":").replace("，", ",").replace("（", "(").replace("）", ")")
        return v.lower()


@dataclass
class FieldVerdict:
    field: str
    value: str
    confidence: float     # 0~1
    sources: list[str]    # 一致源列表
    pending: bool = False # True=置信度不足，需要"待确认"标注

    def as_display(self, show_pending: bool = True) -> str:
        if self.pending:
            return f"{self.value}（待确认）" if show_pending else ""
        return self.value


# ---------- 游戏更新条目 ----------

@dataclass
class GameUpdate:
    game: str                # 游戏标识，如 "wuwa"
    game_display: str        # 展示名，如 "鸣潮"
    version_num: str | None  # 有版本号的填 "4.4"，无版本号 None
    version_name: str        # 版本名/活动名，如 "鸣笛于归寂之时"
    fields: dict[str, FieldVerdict] = field(default_factory=dict)
    raw_urls: list[str] = field(default_factory=list)
    raw_title: str = ""       # 原始公告标题（合并排序用）
    collected_at: str = ""   # 采集时间 ISO

    @property
    def display_title(self) -> str:
        if self.version_num:
            return f"v{self.version_num}「{self.version_name}」"
        return f"「{self.version_name}」"

    @property
    def dedup_key(self) -> str:
        """去重 key：游戏 + 版本标识。"""
        return f"{self.game}:{self.version_num or ''}:{self.version_name}"

    def field_value(self, field: str) -> str:
        v = self.fields.get(field)
        return v.value if v else ""


# ---------- 游戏配置 ----------

@dataclass
class GameConfig:
    key: str                 # 配置文件里的 key，如 "wuwa"
    display: str             # "鸣潮"
    theme_color: str         # 卡片主题色 "#3B82F6"
    adapter: str             # adapter 类型: kuro_json / mihoyo_json / hg_json / hg_ssr
    adapter_params: dict[str, Any] = field(default_factory=dict)
    fields: list[str] = field(default_factory=lambda: ["version", "preview_time", "update_time", "characters", "link"])
    groups: list[str] = field(default_factory=list)  # 空则用默认群
    version_pattern: str = ""  # 从标题提取版本号的正则，如 r"(?P<num>[\d.]+)版本「(?P<name>.+?)」"
    extra: dict[str, Any] = field(default_factory=dict)
    # ---- 版本节奏参数 ----
    cycle_days: int = 42        # 一个大版本的天数（鸣潮 35，米哈游 42）
    half_days: int = 21         # 上半池→下半池切换的天数（版本更新后第 N 天）
    preview_ahead_days: int = 7 # 前瞻直播在版本更新前 N 天
    activity_mode: bool = False # True=活动制（方舟），无版本号，栏位按活动周期展示
    show_link: bool = True      # 卡片是否显示详情链接（方舟去掉）
    known_dates: dict[str, str] = field(default_factory=dict)  # 已官宣的确定时间覆盖，如 {"preview_time": "2026-08-07 19:00"}
    format: str = "version_based"  # 引用 formats/ 下的布局模板名

    @classmethod
    def from_dict(cls, key: str, d: dict[str, Any]) -> "GameConfig":
        extra = dict(d.get("extra", {}))
        # 把顶层筛选规则归入 extra，供 pipeline 读取
        for k in ("title_include", "title_exclude"):
            if k in d:
                extra[k] = d[k]
        return cls(
            key=key,
            display=d.get("display", key),
            theme_color=d.get("theme_color", "#3B82F6"),
            adapter=d["adapter"],
            adapter_params=d.get("adapter_params", {}),
            fields=d.get("fields", ["version", "preview_time", "update_time", "characters", "link"]),
            groups=d.get("groups", []),
            version_pattern=d.get("version_pattern", ""),
            extra=extra,
            cycle_days=int(d.get("cycle_days", 42)),
            half_days=int(d.get("half_days", 21)),
            preview_ahead_days=int(d.get("preview_ahead_days", 7)),
            activity_mode=bool(d.get("activity_mode", False)),
            show_link=bool(d.get("show_link", True)),
            known_dates=dict(d.get("known_dates", {})),
            format=str(d.get("format", "version_based")),
        )
