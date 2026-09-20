from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.tl import functions


async def bump_online(client: TelegramClient, *, hold_seconds: float = 4.0) -> None:
    """
    Mark the account Online briefly, then Offline.

    Updates Telegram last-seen so the profile does not stay on
    «был месяц назад» when nobody opens the session manually.
    """
    await client(functions.account.UpdateStatusRequest(offline=False))
    await asyncio.sleep(max(0.5, float(hold_seconds)))
    try:
        await client(functions.account.UpdateStatusRequest(offline=True))
    except Exception:
        # Online bump already applied; offline is best-effort.
        pass
