from __future__ import annotations

import json
from typing import Any

from telethon.tl import types


def from_aiogram_message(message: Any) -> tuple[str, list[dict[str, Any]]]:
    text = message.text or message.caption or ""
    raw = message.entities or message.caption_entities or []
    out: list[dict[str, Any]] = []
    for ent in raw:
        kind = ent.type
        if hasattr(kind, "value"):
            kind = kind.value
        item: dict[str, Any] = {
            "type": str(kind),
            "offset": int(ent.offset),
            "length": int(ent.length),
        }
        custom_id = getattr(ent, "custom_emoji_id", None)
        if custom_id:
            item["custom_emoji_id"] = str(custom_id)
        url = getattr(ent, "url", None)
        if url:
            item["url"] = url
        user = getattr(ent, "user", None)
        if user is not None:
            item["user_id"] = int(user.id)
        out.append(item)
    return text, out


def entities_loads(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def entities_dumps(entities: list[dict[str, Any]]) -> str:
    return json.dumps(entities, ensure_ascii=False)


def to_telethon_entities(entities: list[dict[str, Any]] | None) -> list[Any]:
    result: list[Any] = []
    for ent in entities or []:
        kind = str(ent.get("type", ""))
        offset = int(ent.get("offset", 0))
        length = int(ent.get("length", 0))
        if kind in {"custom_emoji", "MessageEntityCustomEmoji"}:
            doc = int(ent.get("custom_emoji_id") or ent.get("document_id") or 0)
            result.append(
                types.MessageEntityCustomEmoji(
                    offset=offset, length=length, document_id=doc
                )
            )
        elif kind in {"bold", "MessageEntityBold"}:
            result.append(types.MessageEntityBold(offset=offset, length=length))
        elif kind in {"italic", "MessageEntityItalic"}:
            result.append(types.MessageEntityItalic(offset=offset, length=length))
        elif kind in {"underline", "MessageEntityUnderline"}:
            result.append(types.MessageEntityUnderline(offset=offset, length=length))
        elif kind in {"strikethrough", "strike", "MessageEntityStrike"}:
            result.append(types.MessageEntityStrike(offset=offset, length=length))
        elif kind in {"spoiler", "MessageEntitySpoiler"}:
            result.append(types.MessageEntitySpoiler(offset=offset, length=length))
        elif kind in {"code", "MessageEntityCode"}:
            result.append(types.MessageEntityCode(offset=offset, length=length))
        elif kind in {"pre", "MessageEntityPre"}:
            result.append(
                types.MessageEntityPre(
                    offset=offset, length=length, language=ent.get("language") or ""
                )
            )
        elif kind in {"text_link", "text_url", "MessageEntityTextUrl"}:
            result.append(
                types.MessageEntityTextUrl(
                    offset=offset, length=length, url=ent.get("url") or ""
                )
            )
        elif kind in {"url", "MessageEntityUrl"}:
            result.append(types.MessageEntityUrl(offset=offset, length=length))
        elif kind in {"mention", "MessageEntityMention"}:
            result.append(types.MessageEntityMention(offset=offset, length=length))
        elif kind in {"text_mention", "MessageEntityMentionName"}:
            result.append(
                types.MessageEntityMentionName(
                    offset=offset,
                    length=length,
                    user_id=int(ent.get("user_id") or 0),
                )
            )
        elif kind in {"blockquote", "MessageEntityBlockquote"}:
            result.append(types.MessageEntityBlockquote(offset=offset, length=length))
    return result
