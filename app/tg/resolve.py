from __future__ import annotations

import asyncio
from pathlib import Path

from telethon import TelegramClient

from app.models import Chat
from app.store import Store
from app.tg.client import peer_id, telethon_client


def entity_title(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title).strip()
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    name = f"{first} {last}".strip()
    if name:
        return name
    username = getattr(entity, "username", None)
    if username:
        return str(username)
    return str(peer_id(entity))


def entity_username(entity) -> str:
    return (getattr(entity, "username", None) or "").strip()


async def lookup_entity(client: TelegramClient, chat: Chat):
    targets: list[int | str] = []
    if chat.username:
        targets.append(chat.username)
    targets.append(chat.tg_id)
    seen: set[str] = set()
    for target in targets:
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        try:
            return await client.get_entity(target)
        except Exception:
            continue
    want = str(chat.chat_id).strip()
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        pid = str(peer_id(entity))
        eid = str(getattr(entity, "id", "") or "")
        if want in {pid, eid, f"-100{eid}"}:
            return entity
        title = (getattr(entity, "title", None) or "").strip()
        if chat.title and title and title.casefold() == chat.title.casefold():
            return entity
    return None


async def _session_path(store: Store) -> str | None:
    for acc in await store.list_accounts():
        path = acc.telethon_session
        if path and Path(path).exists():
            return path
    return None


async def refresh_chat_titles(store: Store) -> int:
    session = await _session_path(store)
    if not session:
        return 0
    chats = await store.list_chats()
    if not chats:
        return 0
    updated = 0
    async with telethon_client(session) as client:
        for chat in chats:
            try:
                entity = await lookup_entity(client, chat)
            except Exception:
                continue
            if entity is None:
                continue
            title = entity_title(entity)
            username = entity_username(entity)
            cid = str(peer_id(entity))
            fields: dict[str, str] = {}
            if title and title != chat.title:
                fields["title"] = title
            if username != (chat.username or ""):
                fields["username"] = username
            if cid and cid != chat.chat_id:
                existing = await store.get_chat_by_tg(cid)
                if existing is None or existing.id == chat.id:
                    fields["chat_id"] = cid
            if fields:
                await store.update_chat(chat.id, **fields)
                updated += 1
    return updated


async def resolve_target(store: Store, target: int | str) -> dict[str, str] | None:
    session = await _session_path(store)
    if not session:
        return None
    try:
        async with telethon_client(session) as client:
            entity = await client.get_entity(target)
    except Exception:
        return None
    return {
        "title": entity_title(entity),
        "username": entity_username(entity),
        "chat_id": str(peer_id(entity)),
    }


async def refresh_chat_titles_quiet(store: Store, timeout: float = 25.0) -> int:
    try:
        return await asyncio.wait_for(refresh_chat_titles(store), timeout)
    except Exception:
        return 0
