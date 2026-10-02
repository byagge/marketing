"""Проверка аккаунта через @SpamBot."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from telethon import TelegramClient

SPAMBOT = "SpamBot"

_CLEAN_MARKERS = (
    "no limits are currently applied",
    "good news",
    "свободен от каких-либо ограничений",
    "нет ограничений",
    "ограничений нет",
)
_LIMITED_MARKERS = (
    "limited",
    "restricted",
    "annoying",
    "moderators",
    "anti-spam",
    "ограничен",
    "ограничения",
    "спам",
    "жалоб",
)
_UNTIL_RE = re.compile(
    r"(?:until|till|до)\s+([0-9]{1,2}\s+[A-Za-zА-Яа-я]{3,}\.?\s+[0-9]{4}(?:[,\s]+[0-9]{1,2}:[0-9]{2}(?:\s*UTC)?)?)",
    re.IGNORECASE,
)


@dataclass
class SpamResult:
    status: str  # clean | limited | unknown
    until: str = ""
    detail: str = ""


def parse_spambot_reply(text: str) -> SpamResult:
    raw = (text or "").strip()
    low = raw.casefold()
    if not raw:
        return SpamResult("unknown", detail="пустой ответ")
    if any(m in low for m in _CLEAN_MARKERS) and not any(
        m in low for m in ("is now limited", "now limited", "временно ограничен", "ограничен до")
    ):
        return SpamResult("clean", detail=raw[:300])
    if any(m in low for m in _LIMITED_MARKERS):
        m = _UNTIL_RE.search(raw)
        return SpamResult("limited", until=(m.group(1).strip() if m else ""), detail=raw[:300])
    return SpamResult("unknown", detail=raw[:300])


async def check_spambot(client: TelegramClient, *, wait: float = 4.0) -> SpamResult:
    await client.send_message(SPAMBOT, "/start")
    await asyncio.sleep(wait)
    texts: list[str] = []
    async for msg in client.iter_messages(SPAMBOT, limit=4):
        if getattr(msg, "out", False):
            continue
        if msg.message:
            texts.append(msg.message)
    # свежее первым → берём самое новое непустое, но смотрим все на случай приветствия
    for text in texts:
        res = parse_spambot_reply(text)
        if res.status != "unknown":
            return res
    return parse_spambot_reply("\n".join(texts))
