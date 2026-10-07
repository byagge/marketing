"""Проверка спамблока через @SpamBot."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient

from app.utils.spamstate import SpamStatus, parse_spambot_reply, parse_until

SPAMBOT = "SpamBot"

# Алиас для production-ограничений (chat restrictions / spamblock until).
parse_spam_until = parse_until


@dataclass
class SpamResult:
    """Совместимость со старыми тестами/вызовами: status clean|limited|unknown."""

    status: str
    until: str = ""
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.status in {"clean", "limited"}

    @property
    def limited(self) -> bool:
        return self.status == "limited"

    @property
    def text(self) -> str:
        return self.detail


async def check_spambot(
    client: TelegramClient,
    *,
    wait_sec: float = 12.0,
    poll_sec: float = 1.5,
    wait: float | None = None,
) -> SpamStatus:
    """
    Написать /start в @SpamBot и разобрать ответ.

    Возвращает SpamStatus(known=False), если бот не ответил или текст не распознан —
    состояние аккаунта при этом менять нельзя.

    wait — устаревший алиас wait_sec (совместимость со старыми вызовами).
    """
    if wait is not None:
        wait_sec = float(wait)
    bot: Any = await client.get_entity(SPAMBOT)
    last = await client.get_messages(bot, limit=1)
    last_id = int(last[0].id) if last else 0
    await client.send_message(bot, "/start")
    waited = 0.0
    while waited < wait_sec:
        await asyncio.sleep(poll_sec)
        waited += poll_sec
        async for msg in client.iter_messages(bot, limit=5):
            if int(msg.id) <= last_id or getattr(msg, "out", False):
                continue
            status = parse_spambot_reply(getattr(msg, "message", "") or "")
            if status.known:
                return status
    return SpamStatus(known=False, text="@SpamBot не ответил или ответ не распознан")
