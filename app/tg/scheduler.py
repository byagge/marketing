from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types

from app.utils.entities import to_telethon_entities
from app.utils.schedule import build_schedule_times, posts_count_for_interval

log = logging.getLogger(__name__)

# Ошибки «в этот чат нельзя медиа»
_MEDIA_FORBIDDEN_MARKERS = (
    "chatsendmediaforbidden",
    "chat_send_media_forbidden",
    "chatforbidsforwarding",  # иногда
    "media_invalid",
    "mediaempty",
    "allow_payment_required",  # не то, но
)


def is_media_forbidden_error(exc: BaseException) -> bool:
    name = type(exc).__name__.casefold()
    msg = str(exc).casefold()
    if "media" in name and ("forbidden" in name or "invalid" in name or "empty" in name):
        return True
    return any(m in name or m in msg for m in _MEDIA_FORBIDDEN_MARKERS)


def _extract_sent_message(updates):
    for update in getattr(updates, "updates", []) or []:
        msg = getattr(update, "message", None)
        if msg is not None:
            return msg
    return updates


async def fetch_scheduled(client: TelegramClient, target_entity) -> list:
    while True:
        try:
            result = await client(
                functions.messages.GetScheduledHistoryRequest(peer=target_entity, hash=0)
            )
            return list(getattr(result, "messages", None) or [])
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)


async def delete_scheduled(
    client: TelegramClient,
    target_entity,
    *,
    pause: float = 0.12,
) -> int:
    messages = await fetch_scheduled(client, target_entity)
    if not messages:
        return 0
    ids = [m.id for m in messages]
    deleted = 0
    for start in range(0, len(ids), 100):
        batch = ids[start : start + 100]
        while True:
            try:
                await client(
                    functions.messages.DeleteScheduledMessagesRequest(
                        peer=target_entity, id=batch
                    )
                )
                deleted += len(batch)
                break
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
        await asyncio.sleep(max(0.0, float(pause)))
    return deleted


async def send_scheduled_post(
    client: TelegramClient,
    target_entity,
    text: str,
    entities: list[dict[str, Any]] | None,
    schedule_time: datetime,
    repeat_period: int | None,
    photo_path: str | None = None,
    *,
    allow_media: bool = True,
) -> tuple[Any, bool]:
    """
    Отправить запланированный пост.
    Returns: (sent, used_photo).
    Если медиа запрещено — автоматически без фото.
    """
    tl_entities = to_telethon_entities(entities)
    peer = await client.get_input_entity(target_entity)
    photo = Path(photo_path) if photo_path else None
    want_photo = bool(allow_media and photo is not None and photo.exists())

    async def _send(with_photo: bool) -> Any:
        while True:
            try:
                if with_photo:
                    uploaded = await client.upload_file(str(photo))
                    kwargs: dict[str, Any] = {
                        "peer": peer,
                        "media": types.InputMediaUploadedPhoto(file=uploaded),
                        "message": text or "",
                        "entities": tl_entities or None,
                        "schedule_date": schedule_time,
                    }
                    if repeat_period:
                        kwargs["schedule_repeat_period"] = int(repeat_period)
                    result = await client(functions.messages.SendMediaRequest(**kwargs))
                else:
                    kwargs = {
                        "peer": peer,
                        "message": text or "",
                        "entities": tl_entities or None,
                        "clear_draft": False,
                        "no_webpage": False,
                        "schedule_date": schedule_time,
                    }
                    if repeat_period:
                        kwargs["schedule_repeat_period"] = int(repeat_period)
                    result = await client(functions.messages.SendMessageRequest(**kwargs))
                return _extract_sent_message(result)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)

    if want_photo:
        try:
            return await _send(True), True
        except RPCError as e:
            if is_media_forbidden_error(e):
                log.info("media forbidden → text-only: %s", e)
                return await _send(False), False
            raise
    return await _send(False), False


async def send_scheduled_forward(
    client: TelegramClient,
    *,
    from_peer: Any,
    message_id: int,
    to_peer: Any,
    schedule_time: datetime,
    repeat_period: int | None,
) -> Any:
    """
    Переслать сообщение с сохранением «Переслано из» (drop_author=False),
    чтобы premium emoji отображались у получателей без Premium.
    """
    src = await client.get_input_entity(from_peer)
    dst = await client.get_input_entity(to_peer)
    while True:
        try:
            kwargs: dict[str, Any] = {
                "from_peer": src,
                "id": [int(message_id)],
                "to_peer": dst,
                "schedule_date": schedule_time,
                "drop_author": False,
            }
            if repeat_period:
                kwargs["schedule_repeat_period"] = int(repeat_period)
            result = await client(functions.messages.ForwardMessagesRequest(**kwargs))
            return _extract_sent_message(result)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)


async def schedule_chat_posts(
    client: TelegramClient,
    target: int | str | Any,
    text: str,
    entities: list[dict[str, Any]] | None,
    start_minute: int,
    interval_minutes: int,
    *,
    start_hour: int,
    tz,
    repeat_period: int | None,
    photo_path: str | None = None,
    allow_media: bool = True,
    clear_existing: bool = True,
    pause: float = 0.7,
) -> dict[str, Any]:
    from app.config import get_settings

    settings = get_settings()
    pause = float(getattr(settings, "schedule_pause_sec", pause) or pause)
    del_pause = float(getattr(settings, "schedule_delete_pause_sec", 0.12) or 0.12)

    if isinstance(target, (int, str)):
        entity = await client.get_entity(target)
    else:
        entity = target
    title = (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or str(utils.get_peer_id(entity))
    )
    cleared = 0
    if clear_existing:
        cleared = await delete_scheduled(client, entity, pause=del_pause)

    count = posts_count_for_interval(interval_minutes)
    times = build_schedule_times(
        start_minute=start_minute,
        interval_minutes=interval_minutes,
        posts_count=count,
        tz=tz,
        start_hour=start_hour,
    )

    success = 0
    last_error = ""
    media_blocked = False
    used_photo_any = False
    period = int(repeat_period) if repeat_period else None
    effective_allow = allow_media
    for i, when in enumerate(times, start=1):
        try:
            _sent, used_photo = await send_scheduled_post(
                client=client,
                target_entity=entity,
                text=text,
                entities=entities,
                schedule_time=when,
                repeat_period=period,
                photo_path=photo_path if effective_allow else None,
                allow_media=effective_allow,
            )
            if used_photo:
                used_photo_any = True
            elif photo_path and effective_allow:
                # первый же слот ушёл без фото → дальше не пробуем медиа
                media_blocked = True
                effective_allow = False
            success += 1
            await asyncio.sleep(pause)
        except RPCError as e:
            if is_media_forbidden_error(e) and effective_allow and photo_path:
                media_blocked = True
                effective_allow = False
                try:
                    await send_scheduled_post(
                        client=client,
                        target_entity=entity,
                        text=text,
                        entities=entities,
                        schedule_time=when,
                        repeat_period=period,
                        photo_path=None,
                        allow_media=False,
                    )
                    success += 1
                    await asyncio.sleep(pause)
                    continue
                except RPCError as e2:
                    last_error = f"{type(e2).__name__}: {e2}"
                    break
            last_error = f"{type(e).__name__}: {e}"
            break

    return {
        "title": title,
        "peer_id": int(utils.get_peer_id(entity)),
        "cleared": cleared,
        "planned": len(times),
        "success": success,
        "error": last_error,
        "first_time": times[0].strftime("%d.%m %H:%M") if times else "",
        "start_minute": start_minute,
        "media_blocked": media_blocked,
        "used_photo": used_photo_any,
        "mode": "post",
    }


async def schedule_chat_forwards(
    client: TelegramClient,
    target: int | str | Any,
    *,
    from_peer: int | str | Any,
    message_id: int,
    start_minute: int,
    interval_minutes: int,
    start_hour: int,
    tz,
    repeat_period: int | None,
    clear_existing: bool = True,
    pause: float = 0.7,
) -> dict[str, Any]:
    """Запланировать пересылки одного сообщения (с меткой Forwarded from)."""
    from app.config import get_settings

    settings = get_settings()
    pause = float(getattr(settings, "schedule_pause_sec", pause) or pause)
    del_pause = float(getattr(settings, "schedule_delete_pause_sec", 0.12) or 0.12)

    if isinstance(target, (int, str)):
        entity = await client.get_entity(target)
    else:
        entity = target
    title = (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or str(utils.get_peer_id(entity))
    )
    cleared = 0
    if clear_existing:
        cleared = await delete_scheduled(client, entity, pause=del_pause)

    count = posts_count_for_interval(interval_minutes)
    times = build_schedule_times(
        start_minute=start_minute,
        interval_minutes=interval_minutes,
        posts_count=count,
        tz=tz,
        start_hour=start_hour,
    )

    success = 0
    last_error = ""
    period = int(repeat_period) if repeat_period else None
    for when in times:
        try:
            await send_scheduled_forward(
                client,
                from_peer=from_peer,
                message_id=message_id,
                to_peer=entity,
                schedule_time=when,
                repeat_period=period,
            )
            success += 1
            await asyncio.sleep(pause)
        except RPCError as e:
            last_error = f"{type(e).__name__}: {e}"
            break

    return {
        "title": title,
        "peer_id": int(utils.get_peer_id(entity)),
        "cleared": cleared,
        "planned": len(times),
        "success": success,
        "error": last_error,
        "first_time": times[0].strftime("%d.%m %H:%M") if times else "",
        "start_minute": start_minute,
        "media_blocked": False,
        "used_photo": False,
        "mode": "forward",
    }
