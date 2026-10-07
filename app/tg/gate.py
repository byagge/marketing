"""«Ворота подписки»: бот чата удаляет сообщение аккаунта, упоминает его через @ и
даёт inline-кнопки со ссылками на каналы/чаты, куда нужно подписаться, чтобы писать."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from telethon import TelegramClient

from app.tg.join_flows import (
    _collect_urls_from_message,
    _is_confirm_button,
    split_channels,
    subscribe_required,
)
from app.utils.captcha import extract_inline_buttons, fold_text

# Кнопки «я подписался / проверить» у бота-ворот.
_GATE_CONFIRM_PHRASES = (
    "подписал",
    "подписк",
    "проверить",
    "проверка",
    "готово",
    "вступил",
    "продолжить",
    "check",
    "subscribed",
    "joined",
    "verify",
    "done",
    "continue",
)


def is_gate_confirm_button(label: str) -> bool:
    folded = fold_text(label)
    return bool(folded) and (
        any(p in folded for p in _GATE_CONFIRM_PHRASES) or _is_confirm_button(label)
    )

_PATH_RE = re.compile(r"t(?:elegram)?\.me/([^?#\s]+)", re.IGNORECASE)


@dataclass
class GateInfo:
    message_id: int
    links: list[str]
    confirm: tuple[int, int, str] | None = None
    text: str = ""


@dataclass
class GateResult:
    notes: list[str] = field(default_factory=list)
    subscribed: int = 0
    confirmed: str | None = None


def normalize_gate_link(url: str) -> str | None:
    """
    Привести ссылку к виду, который умеет `subscribe_required`:
    @username для публичных каналов/чатов, полный URL для инвайтов.
    Ссылки на ботов, сообщения (t.me/name/123, t.me/c/…) и папки отбрасываем.
    """
    m = _PATH_RE.search((url or "").strip())
    if not m:
        return None
    path = m.group(1).strip("/")
    low = path.lower()
    if low.startswith("+") or low.startswith("joinchat/"):
        invite = path.split("/", 1)[-1] if low.startswith("joinchat/") else path
        return f"https://t.me/{invite}" if invite.startswith("+") else f"https://t.me/joinchat/{invite}"
    if "/" in path or low.startswith(("c/", "addlist", "share", "proxy", "socks", "addstickers")):
        return None
    name = path.lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", name) or name.lower().endswith("bot"):
        return None
    return f"@{name}"


def merge_require_channels(existing: str, links: list[str]) -> str:
    """Добавить найденные каналы к обязательным (без дублей, регистр username не важен)."""
    items = split_channels(existing)
    seen = {i.casefold().lstrip("@") for i in items}
    for link in links:
        norm = normalize_gate_link(link) or link
        key = norm.casefold().lstrip("@")
        if key and key not in seen:
            seen.add(key)
            items.append(norm)
    return ", ".join(items)


def _mentions_me(message: Any, me_id: int | None, me_username: str | None) -> bool:
    text = (getattr(message, "message", None) or "").casefold()
    if me_username and f"@{me_username.casefold()}" in text:
        return True
    if me_id is not None:
        for ent in getattr(message, "entities", None) or []:
            if getattr(ent, "user_id", None) == me_id:
                return True
    return False


def extract_gate(
    message: Any,
    me_id: int | None,
    me_username: str | None,
    chat_username: str = "",
) -> GateInfo | None:
    """Сообщение-ворота: упоминает аккаунт, есть inline-кнопки и ссылки t.me на каналы."""
    if message is None or not _mentions_me(message, me_id, me_username):
        return None
    buttons = extract_inline_buttons(message)
    if not buttons:
        return None
    own = (chat_username or "").lstrip("@").casefold()
    links: list[str] = []
    for url in _collect_urls_from_message(message):
        norm = normalize_gate_link(url)
        if norm and norm.casefold().lstrip("@") != own and norm not in links:
            links.append(norm)
    if not links:
        return None
    confirm = None
    for ri, ci, label, btn in buttons:
        if not getattr(btn, "url", None) and is_gate_confirm_button(label):
            confirm = (ri, ci, label)
            break
    return GateInfo(
        message_id=int(getattr(message, "id", 0) or 0),
        links=links,
        confirm=confirm,
        text=(getattr(message, "message", None) or "")[:200],
    )


async def scan_gate(
    client: TelegramClient,
    entity: Any,
    me: Any,
    chat_username: str = "",
    *,
    limit: int = 40,
) -> GateInfo | None:
    """Найти в последних сообщениях чата самое свежее сообщение-ворота для аккаунта."""
    me_id = getattr(me, "id", None)
    me_username = getattr(me, "username", None)
    async for msg in client.iter_messages(entity, limit=limit):
        info = extract_gate(msg, me_id, me_username, chat_username)
        if info:
            return info
    return None


def _subscribed(note: str) -> bool:
    return not note.startswith(("fail", "flood"))


async def resolve_gate(client: TelegramClient, entity: Any, info: GateInfo) -> GateResult:
    """Подписаться на все каналы из ворот и, если есть, нажать «я подписался / проверить»."""
    notes = await subscribe_required(client, " ".join(info.links))
    result = GateResult(notes=notes, subscribed=sum(1 for n in notes if _subscribed(n)))
    if info.confirm and result.subscribed:
        await asyncio.sleep(1.0)
        try:
            msg = await client.get_messages(entity, ids=info.message_id)
            if msg is not None:
                ri, ci, label = info.confirm
                try:
                    await msg.click(i=ri, j=ci)
                except Exception:
                    await msg.click(text=label)
                result.confirmed = label[:40]
        except Exception:
            pass
    return result
