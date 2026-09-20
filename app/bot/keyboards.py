from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    DisabledButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.models import Account, Chat, MinuteSlot
from app.ui.emoji import icon_id
from app.utils.minutes import format_minute

BTN_PANEL = "Панель"
BTN_CANCEL = "Отмена"
BTN_HOME = "В меню"


class MenuCB(CallbackData, prefix="m"):
    a: str
    i: int = 0
    p: int = 0


def _kbtn(text: str, icon: str) -> KeyboardButton:
    return KeyboardButton(text=text, icon_custom_emoji_id=icon_id(icon))


def panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[_kbtn(BTN_PANEL, "cube")]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Панель…",
    )


def input_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[_kbtn(BTN_CANCEL, "block")]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Ввод или Отмена",
    )


def ib(text: str, a: str, i: int = 0, p: int = 0, *, icon: str = "cube") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=MenuCB(a=a, i=i, p=p).pack(),
        icon_custom_emoji_id=icon_id(icon),
    )


def _cb(text: str, data: MenuCB, icon: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=data.pack(),
        icon_custom_emoji_id=icon_id(icon),
    )


def _off(text: str, icon: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        icon_custom_emoji_id=icon_id(icon),
        disabled=DisabledButton(),
    )


def home_row() -> list[InlineKeyboardButton]:
    return [ib(BTN_HOME, "home", icon="home")]


def nav_row(action: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    prev = ib("Назад", action, p=page - 1, icon="down") if page > 0 else _off("Назад", "block")
    nxt = (
        ib("Вперёд", action, p=page + 1, icon="up")
        if page + 1 < total_pages
        else _off("Вперёд", "block")
    )
    return [prev, ib(f"{page + 1}/{total_pages}", action, p=page, icon="stack"), nxt]


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Настройка", "setup", icon="robot"), ib("Чаты", "chats", icon="users")],
            [ib("Таблица", "table", icon="clock"), ib("Sender", "sender", icon="link")],
            [ib("Аккаунты", "accounts", icon="user"), ib("Online", "online", icon="star")],
            [ib("Session", "mk_session", icon="inbox")],
            [ib("Отчёты", "reports", icon="chart"), ib("Проверка", "health", icon="search")],
            [ib("Инфо", "info", icon="info")],
        ]
    )


def session_kind_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ib("Telethon", "mk_tl", icon="lock"),
                ib("Pyrogram", "mk_pg", icon="monitor"),
            ],
            [ib(BTN_CANCEL, "cancel", icon="block")],
            home_row(),
        ]
    )


def accounts_kb(accounts: list[Account], page: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[ib("Добавить session", "acc_add", icon="inbox")]]
    chunk = accounts[page * 8 : page * 8 + 8]
    for acc in chunk:
        icon = {"running": "up", "done": "check", "error": "warn"}.get(acc.status, "user")
        rows.append([ib(acc.label, "acc", acc.id, icon=icon)])
    total_pages = max(1, (len(accounts) + 7) // 8)
    if total_pages > 1:
        rows.append(nav_row("accounts", page, total_pages))
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_kb(acc: Account, running: bool = False) -> InlineKeyboardMarkup:
    start = (
        ib("Стоп", "setup_stop", acc.id, icon="down")
        if running
        else ib("Старт", "setup_go", acc.id, icon="up")
    )
    ping_label = "Online ping: вкл" if acc.online_ping_enabled else "Online ping: выкл"
    ping_icon = "check" if acc.online_ping_enabled else "block"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [start],
            [
                ib("Пост", "posts", acc.id, icon="mega"),
                ib("Leave", "leave_go", acc.id, icon="block"),
            ],
            [ib("Проверить schedule", "health_go", acc.id, icon="search")],
            [ib(ping_label, "acc_online", acc.id, icon=ping_icon)],
            [
                ib("Telethon", "acc_tl", acc.id, icon="lock"),
                ib("Pyrogram", "acc_pg", acc.id, icon="monitor"),
            ],
            [
                ib("Sender ID", "acc_sid", acc.id, icon="cube"),
                ib("Sender token", "acc_tok", acc.id, icon="link"),
            ],
            [
                ib("Переименовать", "acc_ren", acc.id, icon="hammer"),
                ib("Удалить", "acc_del", acc.id, icon="warn"),
            ],
            [ib("Аккаунты", "accounts", icon="user")],
            home_row(),
        ]
    )


def online_kb(cfg) -> InlineKeyboardMarkup:
    toggle = (
        ib("Выключить глобально", "on_toggle", icon="block")
        if cfg.enabled
        else ib("Включить глобально", "on_toggle", icon="check")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle],
            [
                ib("Интервал", "on_hours", icon="clock"),
                ib("Пинг сейчас", "on_now", icon="up"),
            ],
            [ib("Аккаунты", "accounts", icon="user")],
            home_row(),
        ]
    )


def confirm_kb(yes: MenuCB, no: MenuCB, yes_text: str = "Запустить") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_cb(yes_text, yes, "check"), _cb(BTN_CANCEL, no, "block")],
            home_row(),
        ]
    )


def posts_kb(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ib("Задать RU", "post_ru", account_id, icon="bookmark"),
                ib("Задать EN", "post_en", account_id, icon="pin"),
            ],
            [ib("Превью с тегом", "post_prev", account_id, icon="search")],
            [ib("Аккаунт", "acc", account_id, icon="user")],
            home_row(),
        ]
    )


def chats_kb(chats: list[Chat], page: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[ib("Добавить чат", "chat_add", icon="inbox")]]
    chunk = chats[page * 8 : page * 8 + 8]
    for chat in chunk:
        if not chat.enabled:
            icon = "block"
        elif chat.is_schedule:
            icon = "clock"
        else:
            icon = "mega"
        title = (chat.display_name or "чат")[:28]
        rows.append(
            [ib(f"{title} {chat.lang} {chat.interval_minutes}m", "chat", chat.id, icon=icon)]
        )
    total_pages = max(1, (len(chats) + 7) // 8)
    if total_pages > 1:
        rows.append(nav_row("chats", page, total_pages))
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chat_kb(chat: Chat) -> InlineKeyboardMarkup:
    rows = [
        [
            ib(
                "schedule" if chat.is_schedule else "sender",
                "chat_kind",
                chat.id,
                icon="clock" if chat.is_schedule else "mega",
            ),
            ib(chat.lang, "chat_lang", chat.id, icon="bookmark"),
        ],
        [
            ib("Тег / гарант", "chat_tag", chat.id, icon="pin"),
            ib("Интервал", "chat_int", chat.id, icon="clock"),
        ],
        [
            ib("Название", "chat_title", chat.id, icon="hammer"),
            ib(
                "Включён" if chat.enabled else "Выключен",
                "chat_on",
                chat.id,
                icon="check" if chat.enabled else "block",
            ),
        ],
    ]
    if chat.is_schedule:
        rows.append([ib("Таблица минут", "tbl", chat.id, icon="clock")])
    rows.append([ib("Удалить", "chat_del", chat.id, icon="warn")])
    rows.append([ib("Чаты", "chats", icon="users")])
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def table_chats_kb(chats: list[Chat]) -> InlineKeyboardMarkup:
    rows = [[ib(c.display_name, "tbl", c.id, icon="clock")] for c in chats]
    rows.append([ib("Скачать Excel", "tbl_dl", icon="inbox")])
    rows.append([ib("Загрузить Excel", "tbl_ul", icon="folder")])
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def table_kb(chat: Chat, slots: list[MinuteSlot], accounts: list[Account]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    used = {s.account_id for s in slots}
    for slot in slots:
        rows.append(
            [
                ib(
                    f":{format_minute(slot.start_minute)}  {slot.account_label}",
                    "tbl_ed",
                    chat.id,
                    slot.account_id,
                    icon="pin",
                )
            ]
        )
    missing = [a for a in accounts if a.id not in used]
    for acc in missing[:6]:
        rows.append([ib(acc.label, "tbl_add", chat.id, acc.id, icon="up")])
    rows.append([ib("Таблицы", "table", icon="clock")])
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Between", "s_between", icon="clock"), ib("Cycle", "s_cycle", icon="stack")],
            [ib("Per-chat", "s_pchat", icon="pin"), ib("Parallel", "s_par", icon="users")],
            [ib("Текст клоакинга", "s_cloak", icon="shield")],
            [ib("Keep extra ids", "s_keep", icon="lock")],
            home_row(),
        ]
    )


def setup_kb(accounts: list[Account], running_ids: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for acc in accounts:
        if acc.id in running_ids:
            rows.append([ib(acc.label, "setup_stop", acc.id, icon="down")])
        else:
            rows.append([ib(acc.label, "setup_ask", acc.id, icon="up")])
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pick_account_kb(accounts: list[Account], action: str) -> InlineKeyboardMarkup:
    rows = [[ib(acc.label, action, acc.id, icon="user")] for acc in accounts]
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib(BTN_CANCEL, "cancel", icon="block")], home_row()]
    )
