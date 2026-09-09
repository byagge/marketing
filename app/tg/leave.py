from __future__ import annotations

import asyncio

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError, UserNotParticipantError
from telethon.tl import functions

from app.tg.client import peer_id


def _dialog_ids(dialog) -> set[int]:
    entity = dialog.entity
    return {int(entity.id), int(utils.get_peer_id(entity))}


def _should_keep(dialog, keep_ids: set[int]) -> bool:
    if dialog.is_user and getattr(dialog.entity, "is_self", False):
        return True
    return bool(keep_ids & _dialog_ids(dialog))


def format_dialog(dialog) -> str:
    entity = dialog.entity
    username = getattr(entity, "username", None)
    uname = f"@{username}" if username else "-"
    kind = "user" if dialog.is_user else "group" if dialog.is_group else "channel"
    return f"{dialog.name} | {kind} | id={utils.get_peer_id(entity)} | {uname}"


async def plan_leave(client: TelegramClient, keep_ids: set[int]) -> tuple[list, list]:
    kept = []
    to_leave = []
    async for dialog in client.iter_dialogs():
        if _should_keep(dialog, keep_ids):
            kept.append(dialog)
        else:
            to_leave.append(dialog)
    return kept, to_leave


async def leave_or_delete(client: TelegramClient, dialog) -> None:
    entity = dialog.entity
    if dialog.is_channel:
        try:
            await client(functions.channels.LeaveChannelRequest(entity))
        except UserNotParticipantError:
            pass
        await client.delete_dialog(entity)
        return
    if dialog.is_group:
        try:
            await client(
                functions.messages.DeleteChatUserRequest(chat_id=entity.id, user_id="me")
            )
        except RPCError:
            pass
        await client.delete_dialog(entity)
        return
    await client.delete_dialog(entity)


async def execute_leave(
    client: TelegramClient,
    keep_ids: set[int],
    progress=None,
) -> dict:
    kept, to_leave = await plan_leave(client, keep_ids)
    ok = 0
    fail = 0
    errors: list[str] = []
    total = len(to_leave)
    for i, dialog in enumerate(to_leave, 1):
        try:
            await leave_or_delete(client, dialog)
            ok += 1
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            try:
                await leave_or_delete(client, dialog)
                ok += 1
            except Exception as e2:
                fail += 1
                errors.append(f"{dialog.name}: {e2}")
        except Exception as e:
            fail += 1
            errors.append(f"{dialog.name}: {e}")
        if progress:
            await progress(i, total, dialog.name)
        await asyncio.sleep(0.35)
    return {
        "kept": [format_dialog(d) for d in kept],
        "left_ok": ok,
        "left_fail": fail,
        "total": total,
        "errors": errors[:15],
    }


# re-export for callers that want peer helper
__all__ = ["execute_leave", "format_dialog", "plan_leave", "peer_id"]
