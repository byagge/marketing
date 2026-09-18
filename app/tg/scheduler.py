from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types

from app.utils.entities import to_telethon_entities
from app.utils.schedule import build_schedule_times, posts_count_for_interval


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


async def delete_scheduled(client: TelegramClient, target_entity) -> int:
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
        await asyncio.sleep(0.4)
    return deleted


async def send_scheduled_post(
    client: TelegramClient,
    target_entity,
    text: str,
    entities: list[dict[str, Any]] | None,
    schedule_time: datetime,
    repeat_period: int | None,
    photo_path: str | None = None,
):
    tl_entities = to_telethon_entities(entities)
    peer = await client.get_input_entity(target_entity)
    photo = Path(photo_path) if photo_path else None
    has_photo = photo is not None and photo.exists()

    while True:
        try:
            if has_photo:
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
    clear_existing: bool = True,
    pause: float = 0.7,
) -> dict[str, Any]:
    # Accept a resolved Telethon entity or a raw id/username.
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
        cleared = await delete_scheduled(client, entity)

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
    for i, when in enumerate(times, start=1):
        try:
            await send_scheduled_post(
                client=client,
                target_entity=entity,
                text=text,
                entities=entities,
                schedule_time=when,
                repeat_period=period,
                photo_path=photo_path,
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
    }
