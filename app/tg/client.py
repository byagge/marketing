from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from telethon import TelegramClient, utils
from telethon.tl.types import User

from app.config import get_settings


def session_stem(path: str | Path) -> str:
    p = Path(path)
    if p.suffix == ".session":
        return str(p.with_suffix(""))
    return str(p)


@asynccontextmanager
async def telethon_client(session_path: str | Path) -> AsyncIterator[TelegramClient]:
    settings = get_settings()
    client = TelegramClient(
        session_stem(session_path),
        settings.api_id,
        settings.api_hash,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Session не авторизована. Загрузите валидный Telethon .session")
        yield client
    finally:
        await client.disconnect()


async def inspect_session(session_path: str | Path) -> dict:
    async with telethon_client(session_path) as client:
        me = await client.get_me()
        assert isinstance(me, User)
        return {
            "user_id": int(me.id),
            "username": me.username or "",
            "phone": me.phone or "",
            "first_name": me.first_name or "",
            "premium": bool(getattr(me, "premium", False)),
        }


def peer_id(entity) -> int:
    return int(utils.get_peer_id(entity))


async def resolve_chat(client: TelegramClient, target: int | str):
    if isinstance(target, str) and target.lstrip("-").isdigit():
        target = int(target)
    return await client.get_entity(target)
