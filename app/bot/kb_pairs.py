"""Клавиатуры: чаты аккаунта (поиск, выключатели), автопилот, баны."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards import MenuCB, _cb, home_row, ib
from app.models import AccountChatPref, Chat, ChatBan
from app.ui.autopilot_screens import pair_mark
from app.utils.chat_search import PAGE_SIZE, clamp_page, page_count, page_slice


def pair_list_kb(
    account_id: int,
    chats: list[Chat],
    prefs: dict[int, AccountChatPref],
    bans: dict[int, ChatBan],
    *,
    page: int,
    query: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    search_row = [ib("Поиск", "acp_search", account_id, icon="search")]
    if query:
        search_row.append(ib("Сбросить поиск", "acp_clear", account_id, icon="block"))
    rows.append(search_row)
    for chat in page_slice(chats, page):
        pref = prefs.get(chat.id)
        mark = pair_mark(pref, bans.get(chat.id))
        edit = " ✎" if pref is not None and pref.has_text else ""
        name = (chat.display_name or "чат")[:28]
        rows.append([ib(f"{mark} {name}{edit}", "acp_open", account_id, chat.id, icon="mega")])
    pages = page_count(len(chats))
    cur = clamp_page(page, len(chats))
    if pages > 1:
        rows.append(
            [
                ib("Назад", "acp_list", account_id, cur - 1, icon="down")
                if cur > 0
                else ib("·", "acp_list", account_id, cur, icon="stack"),
                ib(f"{cur + 1}/{pages}", "acp_list", account_id, cur, icon="stack"),
                ib("Вперёд", "acp_list", account_id, cur + 1, icon="up")
                if cur + 1 < pages
                else ib("·", "acp_list", account_id, cur, icon="stack"),
            ]
        )
    if chats:
        label = "найденные" if query else "все"
        rows.append(
            [
                ib(f"Выкл {label} ({len(chats)})", "acp_bulk", account_id, 0, icon="block"),
                ib(f"Вкл {label} ({len(chats)})", "acp_bulk", account_id, 1, icon="check"),
            ]
        )
    rows.append([ib("Аккаунт", "acc", account_id, icon="user")])
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pair_card_kb(
    account_id: int, chat_pk: int, pref: AccountChatPref, ban: ChatBan | None
) -> InlineKeyboardMarkup:
    rows = [
        [
            ib(
                "Выключить отправку" if pref.enabled else "Включить отправку",
                "acp_tgl",
                account_id,
                chat_pk,
                icon="block" if pref.enabled else "check",
            )
        ],
        [ib("Свой текст для этого чата", "acp_txt", account_id, chat_pk, icon="bookmark")],
    ]
    if pref.has_text:
        rows.append([ib("Убрать свой текст", "acp_txtclr", account_id, chat_pk, icon="warn")])
    if ban is not None and ban.active:
        rows.append([ib("Снять метку бана", "acp_unban", account_id, chat_pk, icon="check")])
    rows.append([ib("К списку чатов", "acp_list", account_id, icon="pin")])
    rows.append([ib("Аккаунт", "acc", account_id, icon="user")])
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bulk_confirm_kb(account_id: int, enable: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _cb("Да", MenuCB(a="acp_bulk_go", i=account_id, p=enable), "check"),
                _cb("Отмена", MenuCB(a="acp_list", i=account_id), "block"),
            ],
            home_row(),
        ]
    )


def autopilot_kb(enabled: bool, running: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            ib(
                "Выключить автопилот" if enabled else "Включить автопилот",
                "ap_toggle",
                icon="block" if enabled else "check",
            )
        ],
        [
            ib(
                "Идёт проход…" if running else "Запустить проход сейчас",
                "ap_now",
                icon="robot",
            )
        ],
        [ib("Сбросить «сдался / вручную»", "ap_reset", icon="hammer")],
        [ib("База банов", "bans", icon="warn"), ib("Стоп-лист чатов", "sl", icon="block")],
        home_row(),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stoplist_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Изменить слова", "sl_edit", icon="hammer")],
            [ib("Применить сейчас ко всем аккаунтам", "sl_apply", icon="up")],
            [ib("Автопилот", "ap", icon="robot")],
            home_row(),
        ]
    )


def bans_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Обновить", "bans", icon="search")],
            [ib("Автопилот", "ap", icon="robot")],
            home_row(),
        ]
    )


def diag_kb(chat_pk: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Проверить вживую (Telethon)", "chat_diag_live", chat_pk, icon="search")],
            [ib("Карточка чата", "chat", chat_pk, icon="mega")],
            home_row(),
        ]
    )


__all__ = [
    "PAGE_SIZE",
    "autopilot_kb",
    "bans_kb",
    "bulk_confirm_kb",
    "diag_kb",
    "pair_card_kb",
    "pair_list_kb",
    "stoplist_kb",
]
