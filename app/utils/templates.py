"""Подстановка {{GARANT}} / {{TAG}} с пересчётом UTF-16 offset entities."""

from __future__ import annotations

import re
from typing import Any, Mapping

from app.models import Chat, Post

PLACEHOLDER_RE = re.compile(r"\{\{\s*(GARANT|TAG)\s*\}\}", re.IGNORECASE)
SHORT_POST_MAX_CHARS = 500


def short_lang(lang: str) -> str:
    base = (lang or "ru").split("_", 1)[0].casefold() or "ru"
    if base not in {"ru", "en"}:
        base = "ru"
    return f"{base}_short"


def pick_post_for_chat(posts: Mapping[str, Post], chat: Chat) -> Post:
    """Выбрать полный или короткий пост по настройке чата."""
    lang = (chat.lang or "ru").split("_", 1)[0].casefold() or "ru"
    if chat.uses_short_text:
        for key in (short_lang(lang), "ru_short", "en_short"):
            post = posts.get(key)
            if post and (post.text or "").strip():
                return post
    post = posts.get(lang) or posts.get("ru")
    if post is not None:
        return post
    return Post(lang=lang)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _shift_entities(
    entities: list[dict[str, Any]],
    utf_start: int,
    utf_end: int,
    delta: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in entities:
        ent = dict(raw)
        offset = int(ent.get("offset", 0))
        length = int(ent.get("length", 0))
        end = offset + length
        if end <= utf_start:
            out.append(ent)
        elif offset >= utf_end:
            ent["offset"] = offset + delta
            out.append(ent)
        else:
            continue
    return out


def render_post(
    text: str,
    entities: list[dict[str, Any]] | None,
    tag: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Заменяет плейсхолдеры. Нет тега — плейсхолдер выкидывается (пропуск)."""
    ents = [dict(e) for e in (entities or [])]
    value = (tag or "").strip()
    matches = list(PLACEHOLDER_RE.finditer(text))
    result = text

    for match in reversed(matches):
        start, end = match.start(), match.end()
        replacement = value
        u_start = utf16_len(result[:start])
        u_old = utf16_len(result[start:end])
        delta = utf16_len(replacement) - u_old
        ents = _shift_entities(ents, u_start, u_start + u_old, delta)
        result = result[:start] + replacement + result[end:]

    if not value:
        result = re.sub(r"[ \t]+\n", "\n", result)
        result = re.sub(r"\n{3,}", "\n\n", result).strip("\n")

    return result, ents
