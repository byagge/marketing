"""PNG минутной таблицы: строки = минуты, колонки = названия чатов, ячейка = аккаунт."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from app.models import Account, Chat, MinuteSlot
from app.utils.minutes import format_minute, period_for_interval, suggest_minute

BG = (255, 242, 204)
HEADER = (31, 78, 121)
HEADER_FG = (255, 255, 255)
GRID = (201, 187, 140)
CELL = (255, 242, 204)
FILLED = (189, 215, 238)
FILLED_TEXT = (15, 55, 110)
MUTED = (90, 90, 90)
TEXT = (40, 40, 40)

_FONT_DIRS = (
    Path(r"C:\Windows\Fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/liberation"),
)
_REGULAR = ("segoeui.ttf", "arial.ttf", "tahoma.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf")
_BOLD = ("segoeuib.ttf", "arialbd.ttf", "tahomabd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf")


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    names = _BOLD if bold else _REGULAR
    for folder in _FONT_DIRS:
        for name in names:
            path = folder / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _fit(text: str, max_chars: int) -> str:
    text = (text or "").replace("\n", " ").strip() or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _text_w(font, text: str) -> float:
    if hasattr(font, "getlength"):
        return font.getlength(text)
    return len(text) * 7


def render_overview_png(
    chats: list[Chat],
    accounts: list[Account],
    slots: list[MinuteSlot],
) -> bytes:
    from PIL import Image, ImageDraw

    cols = chats[:20]
    acc_by_id = {a.id: a for a in accounts}
    cell_at = {(s.chat_pk, s.start_minute): s for s in slots}
    minutes = sorted({s.start_minute for s in slots})
    if not minutes:
        minutes = [0, 15, 30, 45]

    min_w, col_w, row_h = 118, 86, 30
    pad = 18
    width = pad * 2 + min_w + max(len(cols), 1) * col_w
    height = pad * 2 + row_h * (1 + len(minutes)) + 28
    width = min(max(width, 640), 1800)
    height = min(max(height, 220), 2000)

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    head_font = _font(12, True)
    cell_font = _font(13, True)
    min_font = _font(13, True)

    def cell_box(i: int, j: int) -> tuple[int, int, int, int]:
        x0 = pad + (0 if j == 0 else min_w + (j - 1) * col_w)
        y0 = pad + i * row_h
        w = min_w if j == 0 else col_w
        return x0, y0, x0 + w, y0 + row_h

    headers = ["Минута / чат"] + [_fit(c.display_name, 11) for c in cols]
    if not cols:
        headers.append("нет чатов")

    for j, head in enumerate(headers):
        x0, y0, x1, y1 = cell_box(0, j)
        draw.rectangle((x0, y0, x1, y1), fill=HEADER, outline=GRID)
        tw = _text_w(head_font, head)
        draw.text((x0 + (x1 - x0 - tw) / 2, y0 + 7), head, font=head_font, fill=HEADER_FG)

    for i, minute in enumerate(minutes, start=1):
        x0, y0, x1, y1 = cell_box(i, 0)
        draw.rectangle((x0, y0, x1, y1), fill=CELL, outline=GRID)
        label = str(minute)
        tw = _text_w(min_font, label)
        draw.text((x0 + (x1 - x0 - tw) / 2, y0 + 6), label, font=min_font, fill=TEXT)
        for j, chat in enumerate(cols, start=1):
            slot = cell_at.get((chat.id, minute))
            cx0, cy0, cx1, cy1 = cell_box(i, j)
            if slot:
                acc = acc_by_id.get(slot.account_id)
                name = _fit(acc.label if acc else slot.account_label, 10)
                draw.rectangle((cx0, cy0, cx1, cy1), fill=FILLED, outline=GRID)
                tw = _text_w(cell_font, name)
                draw.text((cx0 + (cx1 - cx0 - tw) / 2, cy0 + 6), name, font=cell_font, fill=FILLED_TEXT)
            else:
                draw.rectangle((cx0, cy0, cx1, cy1), fill=CELL, outline=GRID)
        if not cols:
            cx0, cy0, cx1, cy1 = cell_box(i, 1)
            draw.rectangle((cx0, cy0, cx1, cy1), fill=CELL, outline=GRID)

    extra = []
    if len(chats) > len(cols):
        extra.append(f"ещё чатов: {len(chats) - len(cols)}")
    footer = extra[0] if extra else "Колонка — название чата, ячейка — аккаунт"
    draw.text((pad, height - 22), footer, font=_font(12), fill=MUTED)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_chat_png(chat: Chat, slots: list[MinuteSlot], accounts: list[Account]) -> bytes:
    from PIL import Image, ImageDraw

    period = period_for_interval(chat.interval_minutes)
    occupied = [s.start_minute for s in slots]
    try:
        nxt = format_minute(suggest_minute(period, occupied))
    except Exception:
        nxt = "нет"

    acc_by_id = {a.id: a for a in accounts}
    lines = sorted(slots, key=lambda s: s.start_minute)
    pad, title_h, row_h = 18, 56, 30
    min_w, acc_w = 110, 220
    width = pad * 2 + min_w + acc_w
    height = pad * 2 + title_h + row_h * (1 + max(len(lines), 1)) + 24
    height = min(max(height, 200), 1600)

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    title_font = _font(18, True)
    small = _font(12)
    cell_font = _font(13, True)

    draw.text((pad, pad), _fit(chat.display_name, 28), font=title_font, fill=HEADER)
    draw.text(
        (pad, pad + 28),
        f"интервал {chat.interval_minutes} мин   следующий :{nxt}",
        font=small,
        fill=MUTED,
    )

    y0 = pad + title_h
    headers = ("Минута", "Аккаунт")
    widths = (min_w, acc_w)
    x = pad
    for h, w in zip(headers, widths):
        draw.rectangle((x, y0, x + w, y0 + row_h), fill=HEADER, outline=GRID)
        draw.text((x + 10, y0 + 7), h, font=small, fill=HEADER_FG)
        x += w

    if not lines:
        draw.text((pad, y0 + row_h + 8), "Пусто — первый аккаунт получит :00", font=small, fill=MUTED)
    else:
        for i, slot in enumerate(lines):
            y = y0 + row_h * (i + 1)
            acc_name = acc_by_id.get(slot.account_id).label if acc_by_id.get(slot.account_id) else slot.account_label
            vals = (str(slot.start_minute), _fit(acc_name, 22))
            x = pad
            for j, (val, w) in enumerate(zip(vals, widths)):
                fill = FILLED if j == 1 else CELL
                color = FILLED_TEXT if j == 1 else TEXT
                draw.rectangle((x, y, x + w, y + row_h), fill=fill, outline=GRID)
                draw.text((x + 10, y + 6), val, font=cell_font, fill=color)
                x += w

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def try_overview_png(chats: list[Chat], accounts: list[Account], slots: list[MinuteSlot]) -> bytes | None:
    try:
        return render_overview_png(chats, accounts, slots)
    except Exception:
        return None


def try_chat_png(chat: Chat, slots: list[MinuteSlot], accounts: list[Account]) -> bytes | None:
    try:
        return render_chat_png(chat, slots, accounts)
    except Exception:
        return None
