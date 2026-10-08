"""Статистика личных сообщений аккаунта: кто написал, кто написал первым, сколько раз ответил клоакинг."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Служебные пользователи Telegram — не клиенты.
_SERVICE_IDS = {777000, 42777}


@dataclass
class DmStats:
    new_n: int = 0  # написали первыми за окно
    wrote_n: int = 0  # прислали хотя бы одно сообщение за окно
    cloak_n: int = -1  # ответов клоакинга за окно; -1 — текст клоакинга не задан
    scanned: int = 0
    truncated: bool = False


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_client_dialog(dialog: Any) -> bool:
    """Личный чат с живым человеком (не бот, не «Избранное», не служебный аккаунт)."""
    if not getattr(dialog, "is_user", False):
        return False
    ent = getattr(dialog, "entity", None)
    if ent is None:
        return False
    if getattr(ent, "bot", False) or getattr(ent, "deleted", False) or getattr(ent, "is_self", False):
        return False
    return int(getattr(ent, "id", 0) or 0) not in _SERVICE_IDS


async def collect_dm_stats(
    client: Any,
    since: datetime,
    *,
    cloak_text: str = "",
    max_dialogs: int = 400,
    per_dialog: int = 40,
    pause: float = 0.12,
) -> DmStats:
    """
    Обойти личные чаты, в которых что-то происходило после `since`.

    Диалоги идут от свежих к старым, поэтому обход останавливается на первом старом.
    Клоакинг считается по своим исходящим сообщениям, совпадающим с текстом клоакинга.
    """
    since = _aware(since) or datetime.now(timezone.utc)
    cloak = _norm(cloak_text)
    stats = DmStats(cloak_n=0 if cloak else -1)
    async for dialog in client.iter_dialogs():
        last = _aware(getattr(dialog, "date", None))
        if last is not None and last < since:
            break
        if not _is_client_dialog(dialog):
            continue
        if stats.scanned >= max_dialogs:
            stats.truncated = True
            break
        stats.scanned += 1
        ent = dialog.entity
        incoming = False
        async for msg in client.iter_messages(ent, limit=per_dialog):
            when = _aware(getattr(msg, "date", None))
            if when is None or when < since:
                break
            if getattr(msg, "out", False):
                if cloak and _norm(getattr(msg, "message", "") or "") == cloak:
                    stats.cloak_n += 1
            else:
                incoming = True
        if incoming:
            stats.wrote_n += 1
            first = None
            async for msg in client.iter_messages(ent, limit=1, reverse=True):
                first = msg
            if (
                first is not None
                and not getattr(first, "out", False)
                and (_aware(getattr(first, "date", None)) or since) >= since
            ):
                stats.new_n += 1
        await asyncio.sleep(pause)
    return stats
