"""Расширенные сценарии вступления: гарант-бот, заявки, каналы, папки."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
)
from telethon.tl import functions, types

# InviteRequestSentError есть не во всех версиях Telethon
try:
    from telethon.errors import InviteRequestSentError as _InviteRequestSentError
except Exception:  # pragma: no cover
    _InviteRequestSentError = type("InviteRequestSentError", (Exception,), {})

from app.tg.client import peer_id
from app.utils.captcha import (
    extract_inline_buttons,
    fold_text,
    is_verify_button,
    looks_like_captcha,
    pick_verify_button,
    solve_math_captcha,
)

_URL_IN_TEXT = re.compile(r"(https?://t\.me/[^\s\]\)\"']+)", re.I)
_ADDLIST_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/addlist/([A-Za-z0-9_-]+)",
    re.I,
)
_CONFIRM_PHRASES = (
    "я вступил",
    "я вступила",
    "вступил",
    "joined",
    "i joined",
    "готово",
    "done",
    "проверить",
)


def parse_folder_slug(link: str) -> str | None:
    m = _ADDLIST_RE.search(link or "")
    return m.group(1) if m else None


def split_channels(raw: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[,;\s]+", raw or ""):
        part = part.strip()
        if not part:
            continue
        out.append(part)
    return out


def _button_url(btn: Any) -> str | None:
    url = getattr(btn, "url", None)
    if url:
        return str(url)
    # KeyboardButtonUrl / KeyboardButtonCallback — url только у URL-кнопок
    return None


def _collect_urls_from_message(message: Any) -> list[str]:
    urls: list[str] = []
    for _, _, label, btn in extract_inline_buttons(message):
        u = _button_url(btn)
        if u:
            urls.append(u)
        elif label and "t.me/" in label.lower():
            urls.append(label.strip())
    text = getattr(message, "message", None) or ""
    urls.extend(_URL_IN_TEXT.findall(text))
    # entities text_link
    for ent in getattr(message, "entities", None) or []:
        u = getattr(ent, "url", None)
        if u:
            urls.append(str(u))
    # unique keep order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _is_invite_url(url: str) -> bool:
    low = (url or "").lower()
    return ("t.me/+" in low) or ("joinchat/" in low) or ("/addlist/" in low)


def _is_confirm_button(label: str) -> bool:
    folded = fold_text(label)
    return any(p in folded for p in _CONFIRM_PHRASES)


async def ensure_started_with_bot(client: TelegramClient, bot_username: str) -> Any:
    bot = await client.get_entity(bot_username)
    try:
        await client.send_message(bot, "/start")
    except FloodWaitError as e:
        await asyncio.sleep(min(int(e.seconds) + 1, 60))
        await client.send_message(bot, "/start")
    await asyncio.sleep(1.2)
    return bot


async def find_invite_from_bot(
    client: TelegramClient,
    bot_username: str,
    *,
    wait_sec: float = 8.0,
) -> str | None:
    """Пишет /start боту и ищет URL-кнопку / ссылку приглашения."""
    bot = await ensure_started_with_bot(client, bot_username)
    found: list[str] = []

    async def _handler(event: events.NewMessage.Event) -> None:
        if found:
            return
        for url in _collect_urls_from_message(event.message):
            if _is_invite_url(url):
                found.append(url)
                return
        # клик по кнопке без url иногда открывает web — игнор
        # иногда ссылка за обычной callback-кнопкой с текстом «Вступить»
        for _, _, label, btn in extract_inline_buttons(event.message):
            folded = fold_text(label)
            if any(x in folded for x in ("вступ", "join", "чат", "ссылк", "invite")):
                if _button_url(btn):
                    found.append(str(_button_url(btn)))
                    return
                # попробовать кликнуть — бот может прислать ссылку следом
                try:
                    await event.message.click(text=label)
                except Exception:
                    pass

    client.add_event_handler(_handler, events.NewMessage(chats=bot))
    try:
        async for msg in client.iter_messages(bot, limit=12):
            for url in _collect_urls_from_message(msg):
                if _is_invite_url(url):
                    found.append(url)
                    break
            if found:
                break
            # кнопки «получить ссылку» / «вступить»
            for _, _, label, btn in extract_inline_buttons(msg):
                u = _button_url(btn)
                if u and _is_invite_url(u):
                    found.append(u)
                    break
                folded = fold_text(label)
                if any(x in folded for x in ("вступ", "join", "ссылк", "invite", "чат")):
                    try:
                        await msg.click(text=label)
                        await asyncio.sleep(1.5)
                    except Exception:
                        pass
            if found:
                break
        if not found:
            await asyncio.sleep(wait_sec)
    finally:
        try:
            client.remove_event_handler(_handler, events.NewMessage)
        except Exception:
            try:
                client.remove_event_handler(_handler)
            except Exception:
                pass
    return found[0] if found else None


async def click_confirm_in_bot(
    client: TelegramClient,
    bot_username: str,
    *,
    wait_sec: float = 6.0,
) -> str | None:
    """Нажать «Я вступил» у гарант-бота."""
    bot = await client.get_entity(bot_username)
    # иногда нужно снова /start
    try:
        await client.send_message(bot, "/start")
    except Exception:
        pass
    await asyncio.sleep(1.0)

    async def _try_msg(msg: Any) -> str | None:
        for ri, ci, label, _ in extract_inline_buttons(msg):
            if _is_confirm_button(label) or is_verify_button(label):
                try:
                    await msg.click(i=ri, j=ci)
                    return label[:40]
                except Exception:
                    try:
                        await msg.click(text=label)
                        return label[:40]
                    except Exception:
                        continue
        return None

    async for msg in client.iter_messages(bot, limit=10):
        hit = await _try_msg(msg)
        if hit:
            return hit
    await asyncio.sleep(wait_sec)
    async for msg in client.iter_messages(bot, limit=6):
        hit = await _try_msg(msg)
        if hit:
            return hit
    return None


async def solve_bot_math_captcha(
    client: TelegramClient,
    bot_username: str,
    *,
    wait_sec: float = 10.0,
    max_refresh: int = 2,
) -> str | None:
    """
    Капча внутри бота (как LSA_GRNT_BOT):
    «Сколько будет 1 + 1?» + кнопки 5/4/3/2 + «Обновить».
    """
    bot = await ensure_started_with_bot(client, bot_username)
    solved: list[str] = []

    async def _solve_msg(msg: Any) -> str | None:
        text = getattr(msg, "message", None) or ""
        buttons = extract_inline_buttons(msg)
        if not buttons and not looks_like_captcha(text):
            return None
        # обновить если нет числа в кнопках, но есть refresh
        answer = solve_math_captcha(text)
        picked = pick_verify_button(msg, math_answer=answer)
        if picked is None and answer is None:
            for ri, ci, label, _ in buttons:
                folded = fold_text(label)
                if "обнов" in folded or "refresh" in folded or "🔄" in (label or ""):
                    try:
                        await msg.click(i=ri, j=ci)
                        return "refresh"
                    except Exception:
                        try:
                            await msg.click(text=label)
                            return "refresh"
                        except Exception:
                            return None
            return None
        if picked is None:
            return None
        ri, ci, label = picked
        # не кликать «обновить» как ответ
        if "обнов" in fold_text(label) or "refresh" in fold_text(label):
            return None
        try:
            await msg.click(i=ri, j=ci)
            return f"bot_math→{label}"
        except Exception:
            try:
                await msg.click(text=label)
                return f"bot_math→{label}"
            except Exception:
                return None

    refreshes = 0
    deadline = asyncio.get_event_loop().time() + wait_sec
    while asyncio.get_event_loop().time() < deadline and not solved:
        async for msg in client.iter_messages(bot, limit=8):
            detail = await _solve_msg(msg)
            if detail == "refresh":
                refreshes += 1
                if refreshes > max_refresh:
                    break
                await asyncio.sleep(1.2)
                continue
            if detail:
                solved.append(detail)
                break
        if solved:
            break
        await asyncio.sleep(1.0)
    return solved[0] if solved else None


async def subscribe_required(
    client: TelegramClient,
    channels_raw: str,
) -> list[str]:
    """Подписаться на обязательные каналы/чаты."""
    notes: list[str] = []
    for item in split_channels(channels_raw):
        target: Any = item
        try:
            if item.startswith("http") or "t.me/" in item.lower():
                from app.tg.join import extract_invite_hash, extract_public_username

                inv = extract_invite_hash(item)
                pub = extract_public_username(item)
                if inv:
                    try:
                        await client(functions.messages.ImportChatInviteRequest(inv))
                        notes.append(f"channel+{inv[:6]}")
                        continue
                    except UserAlreadyParticipantError:
                        notes.append(f"channel already {inv[:6]}")
                        continue
                if pub:
                    target = pub
            entity = await client.get_entity(target.lstrip("@") if isinstance(target, str) else target)
            if isinstance(entity, types.Channel):
                try:
                    await client(functions.channels.JoinChannelRequest(entity))
                    notes.append(f"@{getattr(entity, 'username', '') or peer_id(entity)}")
                except UserAlreadyParticipantError:
                    notes.append("already")
            await asyncio.sleep(0.4)
        except FloodWaitError as e:
            await asyncio.sleep(min(int(e.seconds) + 1, 90))
            notes.append(f"flood:{e.seconds}")
        except Exception as e:
            notes.append(f"fail:{type(e).__name__}")
    return notes


async def join_via_invite_or_request(
    client: TelegramClient,
    invite_hash: str,
    *,
    as_request: bool = False,
) -> tuple[str, Any]:
    """
    Вступление по инвайту. Заявки: InviteRequestSentError → status request_sent.
    """
    try:
        result = await client(functions.messages.ImportChatInviteRequest(invite_hash))
        chats = getattr(result, "chats", None) or []
        return "joined", chats[0] if chats else None
    except UserAlreadyParticipantError:
        try:
            checked = await client(functions.messages.CheckChatInviteRequest(invite_hash))
            return "already", getattr(checked, "chat", None)
        except Exception:
            return "already", None
    except _InviteRequestSentError:
        return "request_sent", None
    except Exception as e:
        name = type(e).__name__
        if "RequestSent" in name or "invite_request" in str(e).lower():
            return "request_sent", None
        if as_request:
            return "request_sent", None
        raise


async def join_folder(
    client: TelegramClient,
    slug_or_link: str,
    *,
    solve_captcha_fn=None,
) -> dict[str, Any]:
    """
    Вступить во все чаты папки (t.me/addlist/SLUG).
    Затем пройтись по чатам папки и решить капчи.
    """
    slug = parse_folder_slug(slug_or_link) or (slug_or_link or "").strip()
    if not slug:
        return {"ok": False, "error": "нет slug папки", "joined": 0, "captchas": []}

    try:
        from telethon.tl.functions.chatlists import (
            CheckChatlistInviteRequest,
            JoinChatlistInviteRequest,
        )
    except ImportError:
        return {
            "ok": False,
            "error": "Telethon без chatlists API — обновите библиотеку",
            "joined": 0,
            "captchas": [],
        }

    try:
        info = await client(CheckChatlistInviteRequest(slug=slug))
    except Exception as e:
        return {"ok": False, "error": f"check: {type(e).__name__}: {e}", "joined": 0, "captchas": []}

    peers = []
    for attr in ("peers", "already_peers", "missing_peers"):
        val = getattr(info, attr, None)
        if val:
            peers.extend(list(val))

    try:
        await client(
            JoinChatlistInviteRequest(
                slug=slug,
                peers=peers or getattr(info, "peers", []) or [],
            )
        )
        status = "joined_folder"
    except Exception as e:
        # иногда peers обязательны иначе
        try:
            await client(JoinChatlistInviteRequest(slug=slug, peers=[]))
            status = "joined_folder"
        except Exception as e2:
            return {
                "ok": False,
                "error": f"join: {type(e).__name__}/{type(e2).__name__}: {e2}",
                "joined": 0,
                "captchas": [],
            }

    captchas: list[str] = []
    joined_n = len(peers) or 1
    if solve_captcha_fn:
        # капчи в чатах, куда только что вступили
        for peer in peers[:40]:
            try:
                entity = await client.get_entity(peer)
            except Exception:
                continue
            try:
                detail = await solve_captcha_fn(client, entity)
                if detail:
                    captchas.append(detail)
            except Exception:
                continue
            await asyncio.sleep(0.5)

    return {
        "ok": True,
        "status": status,
        "joined": joined_n,
        "captchas": captchas,
        "slug": slug,
    }
