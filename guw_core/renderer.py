"""Pillow 信息卡渲染。

两种输出：
1. render_card：单游戏卡片（调试/单发用）
2. render_summary：多游戏汇总长图（所有游戏纵向排布，一次发送）

布局完全由 formats/*.json 模板驱动，新增游戏只需在 games/*.json 里引用 format，
新增布局类型只需新增一个格式文件，渲染代码不需要改动。

版式（每栏位三行）：
    行1：标签（左） + 日期（右，右上角，预估则黄色）
    行2：大字内容（版本名 / 版本号 / 下半池 / 前瞻 / 复刻·红丝绒）
    行3：干员（浅色小字）
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from guw_core.models import GameConfig, GameUpdate
from guw_core.timeline import TimelineResult

CARD_W = 1080
PAD = 56
SLOT_GAP = 22

_FORMATS_DIR = Path(__file__).resolve().parent.parent / "formats"
_FORMAT_CACHE: dict[str, dict] = {}

# ---------- 字体加载 ----------

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",          # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",        # 黑体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                f = ImageFont.truetype(path, size)
                _FONT_CACHE[size] = f
                return f
            except Exception:
                continue
    f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    """超宽文本截断加省略号（先合并换行为空格）。"""
    text = " ".join(text.split())  # 折叠所有换行/空白
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _accent_rgb(cfg: GameConfig) -> tuple[int, int, int]:
    try:
        accent = cfg.theme_color.lstrip("#")
        return tuple(int(accent[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (59, 130, 246)


def load_format(name: str) -> dict:
    """加载 formats/{name}.json 布局模板（带缓存）。"""
    if name in _FORMAT_CACHE:
        return _FORMAT_CACHE[name]
    path = _FORMATS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"格式模板不存在: {path}")
    fmt = json.loads(path.read_text(encoding="utf-8"))
    _FORMAT_CACHE[name] = fmt
    return fmt


def _fmt_slot(fmt: dict, key: str, default: int) -> int:
    return int(fmt.get("slot", {}).get(key, default))


def _fmt_block(fmt: dict, key: str, default):
    return fmt.get("block", {}).get(key, default)


# ---------- 区块绘制 ----------

# 信息来源小标（P1-3）：official=官方公告 / preview=官方预告 / estimate=周期推算
_SOURCE_TAGS = {"official": "官方", "preview": "预告", "estimate": "推算"}


def _draw_slot_card(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                    slot, accent_rgb: tuple[int, int, int],
                    content_x: int, content_w: int, fmt: dict) -> None:
    """画一个栏位卡片，布局参数来自格式模板。"""
    card = fmt.get("card", {})
    radius = int(card.get("radius", 16))
    fill = card.get("fill", "#171B23")
    outline = card.get("outline", "#2A3140")

    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill, outline=outline, width=1)
    if card.get("accent_bar", True):
        draw.rounded_rectangle([x, y + 24, x + 5, y + h - 24], radius=2, fill=accent_rgb)

    # 行1：标签（左）+ 日期（右，右上角）+ 来源小标（日期左侧，小号弱化色）
    label_font = _load_font(_fmt_slot(fmt, "label_font", 28))
    draw.text((content_x, y + 18), slot.label, font=label_font, fill="#8A93A6")
    if slot.date:
        date_color = "#F5C25A" if slot.estimated else "#C9D1DE"
        date_text = slot.date  # 预估不再追加"（预估）"后缀，改由左侧来源小标表达
        tag = _SOURCE_TAGS.get(slot.source, "")
        tag_font = _load_font(22)
        right = x + w - 28
        date_w = draw.textlength(date_text, font=label_font)
        if tag:
            tag_color = "#F5C25A" if slot.estimated else "#5B6472"
            tag_w = draw.textlength(tag, font=tag_font)
            draw.text((right - tag_w - 10 - date_w, y + 23), tag, font=tag_font, fill=tag_color)
        draw.text((right - date_w, y + 18), date_text, font=label_font, fill=date_color)

    # 行2：大字内容
    main_font = _load_font(_fmt_slot(fmt, "main_font", 38))
    main_color = "#F5C25A" if slot.estimated else "#F5F7FA"
    draw.text((content_x, y + 62), _fit_text(draw, slot.main, main_font, content_w),
              font=main_font, fill=main_color)

    # 行3：干员
    if slot.chars and _fmt_slot(fmt, "show_chars", 1):
        chars_font = _load_font(_fmt_slot(fmt, "chars_font", 30))
        draw.text((content_x, y + 120), _fit_text(draw, slot.chars, chars_font, content_w),
                  font=chars_font, fill="#AAB3C2")


def _draw_game_block(draw: ImageDraw.ImageDraw, x: int, y: int, w: int,
                     update: GameUpdate, cfg: GameConfig, timeline: TimelineResult,
                     content_x: int, content_w: int, fmt: dict) -> int:
    """画一个游戏的区块，返回结束 y。布局由格式模板驱动。"""
    accent_rgb = _accent_rgb(cfg)
    inner_w = w

    # 游戏名（主题色小标题）
    if _fmt_block(fmt, "show_game_name", True):
        draw.text((content_x, y), update.game_display, font=_load_font(_fmt_block(fmt, "game_name_font", 32)), fill=accent_rgb)
        y += 44

    # 版本标题（白）
    if _fmt_block(fmt, "show_version_title", True):
        draw.text((content_x, y), update.display_title, font=_load_font(_fmt_block(fmt, "version_title_font", 40)), fill="#F5F7FA")
        y += 58

    # 栏位卡片
    slot_h = _fmt_slot(fmt, "height", 168)
    for slot in timeline.slots:
        _draw_slot_card(draw, x, y, inner_w, slot_h, slot, accent_rgb, content_x, content_w, fmt)
        y += slot_h + SLOT_GAP
    y -= SLOT_GAP

    return y


# ---------- 单游戏卡片 ----------

def render_card(update: GameUpdate, cfg: GameConfig, timeline: TimelineResult, out_path: Path,
                watermark: str = "") -> Path:
    """渲染单游戏信息卡为 PNG（调试/单发用）。"""
    fmt = load_format(cfg.format)
    PAD2 = PAD
    inner_w = CARD_W - PAD2 * 2
    content_x = PAD2 + 28
    content_w = inner_w - 56
    slot_h = _fmt_slot(fmt, "height", 168)

    header_h = 104
    title_h = 88
    body_h = slot_h * len(timeline.slots) + SLOT_GAP * max(0, len(timeline.slots) - 1)
    footer_h = 84
    H = header_h + title_h + body_h + footer_h + PAD2 * 2

    img = Image.new("RGB", (CARD_W, H), "#0F1115")
    draw = ImageDraw.Draw(img)

    accent_rgb = _accent_rgb(cfg)
    draw.rectangle([0, 0, CARD_W, 10], fill=accent_rgb)

    # 游戏名
    draw.rounded_rectangle([PAD2, PAD2 - 8, PAD2 + 8, PAD2 + 40], radius=4, fill=accent_rgb)
    draw.text((PAD2 + 22, PAD2 - 8), update.game_display, font=_load_font(46), fill=accent_rgb)

    # 版本标题
    draw.text((PAD2, PAD2 + header_h - 16), update.display_title, font=_load_font(40), fill="#F5F7FA")

    # 栏位
    y = PAD2 + header_h + title_h
    for slot in timeline.slots:
        _draw_slot_card(draw, PAD2, y, inner_w, slot_h, slot, accent_rgb, content_x, content_w, fmt)
        y += slot_h + SLOT_GAP

    # 底部
    footer_y = H - PAD2 - 30
    if timeline.note:
        draw.text((PAD2, footer_y), _fit_text(draw, timeline.note, _load_font(24), inner_w - 300),
                  font=_load_font(24), fill="#5B6472")
    info = f"采集于 {update.collected_at[:16]}"
    if watermark:
        info += f" · {watermark}"
    draw.text((CARD_W - PAD2 - draw.textlength(info, font=_load_font(24)), footer_y),
              info, font=_load_font(24), fill="#5B6472")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), "PNG")
    return out_path


# ---------- 多游戏汇总长图 ----------

def render_summary(entries: list[tuple[GameUpdate, GameConfig, TimelineResult]],
                   out_path: Path, watermark: str = "", status_line: str = "") -> Path:
    """渲染多游戏汇总长图：每款游戏一个区块纵向排布，一次发送。

    entries: [(update, cfg, timeline), ...] 按展示顺序
    status_line: 数据源状态行（P1-3），画在底部注释上方，空则不画。
    区块结构完全由各游戏的 format 模板驱动，新增游戏无需改代码。
    """
    PAD2 = PAD
    inner_w = CARD_W - PAD2 * 2
    content_x = PAD2 + 28
    content_w = inner_w - 56

    title_h = 120            # 顶部总标题区
    footer_h = 120 if status_line else 90

    # 先计算总高度（每个游戏用自己的 format 参数）
    body_h = 0
    for _, cfg, tl in entries:
        fmt = load_format(cfg.format)
        block_gap = int(_fmt_block(fmt, "block_gap", 44))
        slot_h = _fmt_slot(fmt, "height", 168)
        h = 0
        if _fmt_block(fmt, "show_game_name", True):
            h += 44
        if _fmt_block(fmt, "show_version_title", True):
            h += 58
        h += slot_h * len(tl.slots) + SLOT_GAP * max(0, len(tl.slots) - 1)
        h += block_gap
        body_h += h
    if entries:
        last_fmt = load_format(entries[-1][1].format)
        body_h -= int(_fmt_block(last_fmt, "block_gap", 44))

    H = title_h + body_h + footer_h + PAD2 * 2
    img = Image.new("RGB", (CARD_W, H), "#0F1115")
    draw = ImageDraw.Draw(img)

    # 顶部：总标题 + 日期
    draw.rectangle([0, 0, CARD_W, 10], fill=(110, 120, 140))
    draw.text((PAD2, PAD2 - 4), "游戏更新速报", font=_load_font(44), fill="#F5F7FA")
    date_str = entries[0][0].collected_at[:16] if entries else ""
    draw.text((CARD_W - PAD2 - draw.textlength(date_str, font=_load_font(28)), PAD2 + 6),
              date_str, font=_load_font(28), fill="#8A93A6")

    # 游戏区块
    y = PAD2 + title_h
    for update, cfg, tl in entries:
        fmt = load_format(cfg.format)
        y = _draw_game_block(draw, PAD2, y, inner_w, update, cfg, tl, content_x, content_w, fmt)
        y += int(_fmt_block(fmt, "block_gap", 44))

    # 底部：数据源状态行 + 生成信息（来源已由各栏位小标表达，不再重复画旧 note）
    footer_y = H - PAD2 - 34
    if status_line:
        draw.text((PAD2, footer_y - 34), _fit_text(draw, status_line, _load_font(24), inner_w - 60),
                  font=_load_font(24), fill="#5B6472")
    info = "由 game-update-watcher 生成"
    if watermark:
        info += f" · {watermark}"
    draw.text((CARD_W - PAD2 - draw.textlength(info, font=_load_font(24)), footer_y),
              info, font=_load_font(24), fill="#5B6472")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), "PNG")
    return out_path
