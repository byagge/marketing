"""Пуш поста/клоакинга в Autoposter с сохранением premium emoji и переносов."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from app.sender_api import SenderAPI, SenderAPIError
from app.utils.entities import to_telethon_entities


def has_premium_emoji(entities: list[dict[str, Any]] | None) -> bool:
    for ent in entities or []:
        kind = str(ent.get("type", "")).lower()
        if kind in {"custom_emoji", "messageentitycustomemoji"}:
            return True
        if "emoji" in kind and (ent.get("custom_emoji_id") or ent.get("document_id")):
            return True
    return False


def normalize_multiline(text: str) -> str:
    """Сохраняет Enter/абзацы; убирает только хвост пробелов по строкам."""
    if not text:
        return ""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


async def _put_post_safe(
    api: SenderAPI,
    sender_id: str,
    text: str,
    *,
    photo_bytes: bytes | None = None,
    mode: str = "post",
    link_chat_id: int | None = None,
    link_msg_id: int | None = None,
) -> dict[str, Any]:
    """Autoposter не принимает entities — шлём только поддерживаемые поля."""
    try:
        return await api.put_post(
            sender_id,
            text,
            photo_bytes,
            mode=mode,
            link_chat_id=link_chat_id,
            link_msg_id=link_msg_id,
        )
    except SenderAPIError as e:
        if e.status != 422:
            raise
        return await api.put_post(sender_id, text, photo_bytes, mode=mode)


async def push_sender_post(
    api: SenderAPI,
    sender_id: str,
    text: str,
    entities: list[dict[str, Any]] | None,
    photo_bytes: bytes | None = None,
    *,
    telethon_session: str | None = None,
) -> dict[str, Any]:
    """
    Передаёт пост в Autoposter.

    Premium emoji через plain API не живут — если есть Telethon session того же
    аккаунта, кладём сообщение в «Избранное» и ставим mode=link на него.
    """
    body_text = normalize_multiline(text)
    ents = list(entities or [])

    if has_premium_emoji(ents) and telethon_session and Path(telethon_session).exists():
        from app.tg.client import telethon_client

        async with telethon_client(telethon_session) as client:
            me = await client.get_me()
            tl_ents = to_telethon_entities(ents) or None
            if photo_bytes:
                buf = BytesIO(photo_bytes)
                buf.name = "post.jpg"
                send_kw: dict[str, Any] = {"caption": body_text}
                if tl_ents:
                    send_kw["formatting_entities"] = tl_ents
                msg = await client.send_file("me", buf, **send_kw)
            else:
                send_kw = {}
                if tl_ents:
                    send_kw["formatting_entities"] = tl_ents
                msg = await client.send_message("me", body_text, **send_kw)
            return await _put_post_safe(
                api,
                sender_id,
                body_text,
                photo_bytes=None,
                mode="link",
                link_chat_id=int(me.id),
                link_msg_id=int(msg.id),
            )

    return await _put_post_safe(
        api,
        sender_id,
        body_text,
        photo_bytes=photo_bytes,
        mode="post",
    )


async def push_cloak(
    api: SenderAPI,
    sender_id: str,
    *,
    enabled: bool,
    text: str,
) -> dict[str, Any]:
    """Полный пуш клоакинга на один sender-аккаунт + проверка."""
    body = normalize_multiline(text)
    on = bool(enabled and body.strip())
    await api.put_cloak(sender_id, on, body if on else "")
    try:
        got = await api.get_cloak(sender_id)
    except SenderAPIError:
        return {"ok": True, "enabled": on, "verified": False, "text_len": len(body)}
    got_on = bool(got.get("enabled"))
    got_text = normalize_multiline(str(got.get("text") or ""))
    ok = (got_on == on) and ((got_text == body) if on else True)
    if not ok and on:
        # повтор, если Autoposter не принял с первого раза
        await api.put_cloak(sender_id, True, body)
        got = await api.get_cloak(sender_id)
        got_on = bool(got.get("enabled"))
        got_text = normalize_multiline(str(got.get("text") or ""))
        ok = got_on and got_text == body
    return {
        "ok": ok,
        "enabled": on,
        "verified": True,
        "text_len": len(body),
        "remote_enabled": got_on,
        "remote_len": len(got_text),
    }


async def disable_mentions_everywhere(
    api: SenderAPI,
    sender_id: str,
    live_chats: list[dict[str, Any]] | None = None,
) -> int:
    """
    Выключить глобальные отметки аккаунта и mention у каждого чата.
    Autoposter по умолчанию часто ставит mention=\"global\" — это и есть
    «глобальная отметка во всех чатах».
    """
    await api.put_mentions(sender_id, False)
    chats = live_chats if live_chats is not None else await api.list_chats(sender_id)
    patched = 0
    for live in chats:
        cid = str(live.get("chat_id") or "")
        if not cid:
            continue
        try:
            await api.patch_chat(sender_id, cid, mention=False)
            patched += 1
        except SenderAPIError:
            try:
                await api.patch_chat(sender_id, cid, mention=False)
            except SenderAPIError:
                continue
    return patched


async def push_chat_text(
    api: SenderAPI,
    sender_id: str,
    chat_id: str,
    text: str,
    entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Текст чата: mode=post, упоминания выкл, active=true."""
    del entities  # Autoposter entities в PATCH не принимает
    body = normalize_multiline(text)
    payload = {
        "active": True,
        "mode": "post",
        "text": body,
        "mention": False,  # не "global" и не true
    }
    try:
        return await api.patch_chat(sender_id, chat_id, **payload)
    except SenderAPIError as e:
        if e.status != 422:
            raise
        # fallback по шагам: сначала mention off, потом текст
        try:
            await api.patch_chat(sender_id, chat_id, mention=False)
        except SenderAPIError:
            pass
        return await api.patch_chat(sender_id, chat_id, active=True, text=body)
