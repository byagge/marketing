"""Парсинг ссылок на сообщения Telegram (t.me/… / t.me/c/…)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

# https://t.me/c/1234567890/42
# https://t.me/username/42
# https://t.me/username/42?single
_MSG_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:c/(\d+)/(\d+)|([A-Za-z0-9_]+)/(\d+))",
    re.I,
)


def parse_message_link(url: str) -> dict[str, Any] | None:
    """
    Вернуть {chat: int|-100…|@user, msg_id: int, url: str} или None.
    Для t.me/c/ID/MSG → chat_id = -100{ID}.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    # иногда присылают без схемы
    if "://" not in raw and raw.startswith("t.me"):
        raw = "https://" + raw
    m = _MSG_LINK_RE.search(raw)
    if not m:
        return None
    if m.group(1) and m.group(2):
        internal = m.group(1)
        msg_id = int(m.group(2))
        chat_id = int(f"-100{internal}")
        return {"chat": chat_id, "msg_id": msg_id, "url": raw.split("?")[0]}
    username = m.group(3)
    msg_id = int(m.group(4))
    if username.casefold() in {"c", "joinchat", "addlist", "share", "proxy", "socks"}:
        return None
    return {"chat": username, "msg_id": msg_id, "url": raw.split("?")[0]}


def format_message_link(chat: Any, msg_id: int) -> str:
    if isinstance(chat, int):
        s = str(abs(chat))
        if s.startswith("100"):
            s = s[3:]
        return f"https://t.me/c/{s}/{int(msg_id)}"
    return f"https://t.me/{str(chat).lstrip('@')}/{int(msg_id)}"
