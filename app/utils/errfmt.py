"""Короткие и понятные тексты ошибок для сводок (без дампов байтов и трейсов)."""

from __future__ import annotations

OUTDATED_TELETHON = (
    "Telethon устарел: Telegram прислал тип, которого библиотека не знает — "
    "обновите на сервере `pip install -U telethon` и перезапустите"
)
NO_ENTITY = (
    "аккаунт не видит этот чат (его нет в диалогах аккаунта, а ссылки вступления "
    "в карточке чата нет)"
)


def short_error(e: BaseException, limit: int = 160) -> str:
    """`ИмяОшибки: сообщение`, обрезанное до limit; известные случаи — по-русски."""
    name = type(e).__name__
    raw = str(e) or ""
    if name == "TypeNotFoundError" or "Could not find a matching Constructor ID" in raw:
        return OUTDATED_TELETHON
    if "Could not find the input entity" in raw:
        return NO_ENTITY
    msg = " ".join(raw.split())
    if len(msg) > limit:
        msg = msg[: limit - 1].rstrip() + "…"
    return f"{name}: {msg}" if msg else name
