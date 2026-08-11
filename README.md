# game-update-watcher

MaiBot 插件：定时采集多款游戏版本更新信息，交叉认证后渲染成信息卡图片，推送到指定 QQ 群。

支持游戏（开箱即用）：鸣潮、明日方舟、明日方舟终末地、绝区零、崩坏：星穹铁道

## 触发方式

插件默认通过 **Tool「游戏更新速报」** 触发：麦麦的 LLM 在对话中判断用户需要游戏更新/卡池/前瞻信息时，自动调用工具生成汇总图发送到当前聊天流。

手动触发：群里发送 `/游戏速报`。

可选定时推送：`config.toml` 里 `scheduled_enabled = true` 并填 `default_groups`，按 `poll_interval_minutes` 间隔自动推送新版本信息。

## 工作原理

```
┌─────────┐   ┌──────────┐   ┌──────────┐   ┌────────┐   ┌─────────┐
│ Adapter │ → │ 字段提取  │ → │ 交叉认证  │ → │ 出图    │ → │ 多群发送 │
│ (采集)  │   │ (解析)    │   │ (置信度)  │   │ Pillow │   │ send.image│
└─────────┘   └──────────┘   └──────────┘   └────────┘   └─────────┘
```

- **采集**：4 个 adapter（米哈游 JSON / 鹰角 JSON / 鹰角 SSR / 库洛 JSON），按「数据形态」复用，不按游戏拆分
- **解析**：每游戏一份 `games/*.json`，配置版本号正则、标题过滤规则
- **认证**：字段级置信度 = 最高源权重 + 一致源加分，低于阈值标「待确认」或丢弃
- **出图**：Pillow 绘制 1080 宽信息卡（版本标题、前瞻时间、更新时间、新角色、详情链接）
- **发送**：`ctx.chat.open_session(group)` 打开群聊流 → `ctx.send.image(base64)` 发图，单群失败不阻塞其他群

## 安装

1. 把本目录整个复制到 MaiBot 的 `plugins/` 下（目录名随意，如 `game-update-watcher`）
2. 确认 MaiBot 环境已安装依赖：`pip install maibot-plugin-sdk httpx pillow`
3. 启动 MaiBot，插件自动加载（也可通过 WebUI 管理）

## 配置

### config.toml（插件主配置）

```toml
[plugin]
enabled = true
poll_interval_minutes = 360      # 轮询间隔（分钟）
default_groups = ["123456789"]   # 默认目标 QQ 群号列表（字符串数组）
show_pending_fields = true       # 是否显示「待确认」字段
publish_threshold = 0.8          # 字段置信度发布阈值
http_timeout_seconds = 15
debug = false
```

### games/*.json（每游戏一份）

- `adapter`：使用的采集器（mihoyo_json / hg_json / hg_ssr / kuro_json）
- `version_pattern`：从标题提取版本号+版本名的正则，用命名分组 `(?P<num>)` / `(?P<name>)`
  - 有版本号的游戏：`(?P<num>[\d.]+)版本「(?P<name>.+?)」` → 显示 `v4.4「鸣笛于归寂之时」`
  - 无版本号的游戏（方舟）：留空，自动从「」提取活动名 → 显示 `「直到大地变成一颗酸橙」`
- `title_include` / `title_exclude`：标题命中/排除关键词
- `groups`：该游戏专属目标群，留空则用 `default_groups`

新增游戏：在 `games/` 下加一个 JSON，选一个现有 adapter 填参数即可，不用写代码。

## 自测（不启动 MaiBot）

```powershell
cd plugins/game-update-watcher
python selftest.py            # 全部游戏
python selftest.py wuwa hsr   # 指定游戏
```

会真实请求各厂商接口、打印解析结果，并在 `_selftest_out/` 生成示例信息卡 PNG。

## 出图与发送链路说明

1. **出图**：`core/renderer.py` 用 Pillow 把 `GameUpdate` 画成 1080 宽 PNG，深色底 + 游戏主题色，字段逐行排列
2. **编码**：PNG 读成 base64 字符串
3. **发送**：
   ```python
   session = await self.ctx.chat.open_session(platform="qq", chat_type="group", group_id="123456789")
   stream_id = session.get("stream_id") if isinstance(session, dict) else session
   await self.ctx.send.image(image_data=base64_str, stream_id=stream_id)
   ```

图片是纯本地生成的，不经过任何第三方图床，QQ 群内直接显示。

## 已知限制

- 鸣潮接口 URL 带 hash，官方轮换后需更新 `games/wuwa.json` 里的 `notice_url`（可加 B站官号动态做认证源兜底）
- 终末地是 HTML 解析（无 JSON 接口），官网改版需微调 `hg_ssr.py` 正则
- 新角色提取用的是通用正则，个别角色名可能漏抓或误抓，可后续在 `core/validator.py` 的 `RE_CHARS` 里补充规则

## 目录结构

```
game-update-watcher/
├── _manifest.json        # 插件清单
├── config.toml           # 插件配置
├── plugin.py             # MaiBot 插件入口（定时轮询+发送）
├── selftest.py           # 独立自测脚本
├── verify.py / verify.ps1 # 一键验证
├── GUIDE.md              # 新增游戏接入指南
├── formats/              # 布局模板（按类型）
│   ├── version_based.json    # 版本制：游戏名+版本标题+两栏位
│   └── activity_based.json   # 活动制：游戏名+活动标题+两栏位
├── games/                # 每游戏一份配置（新增游戏看 GUIDE.md）
│   ├── wuwa.json         # 鸣潮
│   ├── arknights.json    # 明日方舟
│   ├── endfield.json     # 终末地
│   ├── zzz.json          # 绝区零
│   └── hsr.json          # 崩铁
└── core/
    ├── adapters/         # 采集层（4 个 adapter）
    ├── models.py         # 数据模型
    ├── validator.py      # 字段提取 + 交叉认证
    ├── timeline.py       # 6 周阶段判定 + 两栏位生成
    ├── pipeline.py       # 管道组装
    ├── renderer.py       # Pillow 出图（格式模板驱动）
    └── store.py          # SQLite 去重
```

## 新增游戏

看 `GUIDE.md`：新建 `games/xxx.json` + 引用已有 format 即可，summary 自动适配，不改代码。
