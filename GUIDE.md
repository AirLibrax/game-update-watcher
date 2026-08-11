# 新增游戏接入指南

接入一款新游戏只需要两步，**不需要改任何代码**：

1. 在 `games/` 下新建一个 JSON 配置文件
2. 选一个已有的 `formats/` 布局模板引用

summary 汇总图会自动从配置构成结构，新增游戏后下次轮询自动出现在图里。

## 标准模板

```json
// games/example.json
{
  "display": "游戏展示名",
  "short": "简称",
  "theme_color": "#FF0000",
  "format": "version_based",
  "adapter": "mihoyo_json",
  "adapter_params": {
    ...
  },
  "version_pattern": "(?P<num>[\\d.]+)版本「(?P<name>.+?)」",
  "title_include": ["版本更新"],
  "title_exclude": ["已知问题", "封禁"],
  "cycle_days": 42,
  "half_days": 21,
  "preview_ahead_days": 7,
  "known_dates": {},
  "groups": []
}
```

## 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `display` | ✅ | 图上显示的游戏名 |
| `short` | ❌ | 简称，预留 |
| `theme_color` | ❌ | 主题色，卡片左侧色条和游戏名颜色，默认蓝 |
| `format` | ❌ | 布局模板名，见 `formats/`，默认 `version_based` |
| `adapter` | ✅ | 采集器，见下方适配器表 |
| `adapter_params` | ✅ | 采集器参数（URL、game_id 等） |
| `version_pattern` | ❌ | 从标题提取版本号的正则，命名分组 `(?P<num>)`/`(?P<name>)` |
| `title_include` | ❌ | 标题必须包含的关键词之一 |
| `title_exclude` | ❌ | 标题包含则排除的关键词 |
| `cycle_days` | ❌ | 版本周期天数，默认 42（鸣潮 35） |
| `half_days` | ❌ | 上半池→下半池切换天数，默认 21 |
| `preview_ahead_days` | ❌ | 前瞻在版本更新前 N 天，默认 7 |
| `known_dates` | ❌ | 已官宣的确定时间覆盖，如 `{"preview_time": "2026-08-07 19:00"}` |
| `groups` | ❌ | 该游戏专属目标群，空则用全局 default_groups |

## 适配器（采集器）

| adapter 名 | 数据形态 | 适用 |
|---|---|---|
| `mihoyo_json` | 米哈游 getAnnList JSON | 崩铁、绝区零、原神 |
| `hg_json` | 鹰角 bulletin JSON | 明日方舟 |
| `hg_ssr` | 鹰角官网 SSR HTML | 终末地 |
| `kuro_json` | 库洛 gamenotice JSON | 鸣潮 |

新增适配器：在 `core/adapters/` 加一个文件，实现 `collect()` 返回原始条目列表，并在 `core/adapters/__init__.py` 的 `ADAPTERS` 字典注册。这一步需要写代码，但只做一次，之后所有游戏复用。

## 布局模板（formats/）

| 模板名 | 结构 |
|---|---|
| `version_based` | 游戏名 + 版本标题 + 两栏位（上半池/下半池/前瞻/下版本） |
| `activity_based` | 游戏名 + 活动标题 + 两栏位（本版本/下版本预告） |

模板文件控制：区块是否显示游戏名/版本标题、字号、栏位高度、卡片圆角/颜色。
新增布局类型：复制一个模板改参数即可，游戏配置 `format` 指向新模板名。

## 已知确定时间（known_dates）

官方已官宣但接口还没推送的时间，写在这里作为确定值（不标预估）：

```json
"known_dates": {
  "preview_time": "2026-08-07 19:00",     // 前瞻直播已定档
  "next_update_time": "2026-08-20",       // 下版本更新已定档
  "half_start": "2026-08-19"              // 下半池开启已定档
}
```

支持字段：`preview_time` / `next_update_time` / `half_start` / `update_time`。
新版本公告上线后接口会自动覆盖这些值，known_dates 可以留着不管（同值去重）。

## 完整示例：新增"原神"

```json
// games/genshin.json
{
  "display": "原神",
  "theme_color": "#00B3B3",
  "format": "version_based",
  "adapter": "mihoyo_json",
  "adapter_params": {
    "api_base": "https://hk4e-api.mihoyo.com",
    "game": "hk4e",
    "game_biz": "hk4e_cn",
    "bundle_id": "hk4e_cn",
    "region": "cn_gf01",
    "level": 55,
    "uid": "100000000",
    "web_base": "https://ys.mihoyo.com/news",
    "timeout": 15
  },
  "version_pattern": "(?P<num>[\\d.]+)版本「(?P<name>.+?)」",
  "title_include": ["版本更新"],
  "title_exclude": ["已知问题", "优化", "修复", "概率公示", "封禁", "祈愿", "武器", "音乐"],
  "cycle_days": 42,
  "half_days": 21,
  "preview_ahead_days": 7
}
```

放好文件重启插件即可，summary 自动出现原神区块。
