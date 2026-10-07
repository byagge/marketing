"""Поиск и постраничный вывод чатов для интерфейса."""

from __future__ import annotations

from app.models import Chat

PAGE_SIZE = 8


def filter_chats(chats: list[Chat], query: str) -> list[Chat]:
    """Поиск по названию, @username, тегу и id; несколько слов — все должны встретиться."""
    words = [w for w in (query or "").casefold().split() if w]
    if not words:
        return list(chats)
    out: list[Chat] = []
    for chat in chats:
        hay = " ".join(
            [
                (chat.display_name or ""),
                (chat.title or ""),
                (chat.username or ""),
                (chat.tag or ""),
                (chat.chat_id or ""),
            ]
        ).casefold()
        if all(w in hay for w in words):
            out.append(chat)
    return out


def page_count(total: int, size: int = PAGE_SIZE) -> int:
    return max(1, (total + size - 1) // size)


def clamp_page(page: int, total: int, size: int = PAGE_SIZE) -> int:
    return min(max(0, page), page_count(total, size) - 1)


def page_slice(items: list, page: int, size: int = PAGE_SIZE) -> list:
    page = clamp_page(page, len(items), size)
    return items[page * size : page * size + size]
