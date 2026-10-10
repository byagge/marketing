from __future__ import annotations

from telethon import TelegramClient, utils

from app.models import Chat
from app.tg.resolve import lookup_entity
from app.tg.scheduler import fetch_scheduled
from app.utils.schedule import posts_count_for_interval
from app.utils.errfmt import short_error


async def check_schedule_chats(
    client: TelegramClient,
    chats: list[Chat],
) -> list[dict]:
    rows: list[dict] = []
    for chat in chats:
        if not chat.is_schedule or not chat.enabled:
            continue
        expected = posts_count_for_interval(chat.interval_minutes)
        try:
            entity = await lookup_entity(client, chat)
            if entity is None:
                raise ValueError("чат не найден в аккаунте")
            scheduled = await fetch_scheduled(client, entity)
            count = len(scheduled)
            if count == 0:
                status = "empty"
            elif count < max(1, expected // 2):
                status = "low"
            else:
                status = "ok"
            title = getattr(entity, "title", None) or chat.title
            peer = utils.get_peer_id(entity)
            error = ""
        except Exception as e:
            count = 0
            status = "error"
            title = chat.title
            peer = chat.chat_id
            error = f"{short_error(e)}"
        rows.append(
            {
                "chat_pk": chat.id,
                "title": title,
                "peer": peer,
                "count": count,
                "expected": expected,
                "status": status,
                "error": error,
            }
        )
    return rows
