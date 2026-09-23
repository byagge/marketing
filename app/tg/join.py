"""Вступление аккаунта в чаты каталога (invite / username / гарант / капча / папка)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
)
from telethon.tl import functions, types

from app.models import Chat
from app.tg.client import peer_id
from app.tg.join_flows import (
    click_confirm_in_bot,
    find_invite_from_bot,
    join_folder,
    join_via_invite_or_request,
    parse_folder_slug,
    solve_bot_math_captcha,
    subscribe_required,
)
from app.tg.join_presets import effective_join_config
from app.tg.resolve import lookup_entity
from app.utils.captcha import (
    extract_inline_buttons,
    looks_like_captcha,
    pick_verify_button,
    solve_math_captcha,
    solve_poll_captcha,
)

_INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,})/?$",
    re.IGNORECASE,
)
_BOT_HINT_RE = re.compile(r"t\.me/[A-Za-z0-9_]+bot(?:\?start=|$)", re.IGNORECASE)


@dataclass
class JoinResult:
    chat_title: str
    chat_pk: int
    status: str
    # joined | already | captcha_ok | request_sent | needs_manual | failed | skipped
    detail: str = ""


@dataclass
class MembershipReport:
    joined: list[Chat] = field(default_factory=list)
    missing: list[Chat] = field(default_factory=list)
    no_link: list[Chat] = field(default_factory=list)


def extract_invite_hash(link: str) -> str | None:
    raw = (link or "").strip()
    if not raw:
        return None
    m = _INVITE_RE.search(raw)
    return m.group(1) if m else None


def extract_public_username(link: str) -> str | None:
    raw = (link or "").strip().lstrip("@")
    if not raw:
        return None
    if raw.startswith("http") or "t.me/" in raw.lower():
        m = _PUBLIC_RE.search(raw.split("?")[0])
        if m:
            name = m.group(1)
            if name.lower() in {"joinchat", "addstickers", "share", "proxy", "socks", "addlist"}:
                return None
            return name
        return None
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", raw):
        return raw
    return None


def needs_bot_flow(link: str) -> bool:
    return bool(_BOT_HINT_RE.search(link or ""))


def chat_join_target(chat: Chat) -> dict[str, str]:
    cfg = effective_join_config(chat)
    if cfg["join_mode"] == "garant" and cfg["garant_bot"]:
        return {"method": "garant", "value": cfg["garant_bot"]}

    link = (cfg.get("invite_link") or getattr(chat, "invite_link", None) or "").strip()
    if parse_folder_slug(link):
        return {"method": "folder", "value": link}
    if needs_bot_flow(link) and not cfg["garant_bot"]:
        return {"method": "manual", "value": link, "reason": "нужен бот / временная ссылка"}
    invite = extract_invite_hash(link)
    if invite:
        return {"method": "invite", "value": invite}
    public = extract_public_username(link) or (chat.username or "").strip().lstrip("@")
    if public:
        return {"method": "username", "value": public}
    raw = (chat.chat_id or "").strip()
    if raw.lstrip("-").isdigit() or raw.startswith("@"):
        return {"method": "chat_id", "value": raw}
    if link:
        return {"method": "manual", "value": link, "reason": "особая ссылка"}
    if cfg["join_mode"] == "garant":
        return {"method": "manual", "value": "", "reason": "не указан гарант-бот"}
    return {"method": "manual", "value": "", "reason": "нет ссылки вступления"}


async def is_member(
    client: TelegramClient,
    chat: Chat,
    *,
    dialog_peer_ids: set[int] | None = None,
) -> bool:
    entity = await lookup_entity(client, chat)
    if entity is None:
        return False
    try:
        if isinstance(entity, types.Channel):
            await client(functions.channels.GetParticipantRequest(entity, "me"))
            return True
    except Exception:
        return False

    try:
        pid = int(peer_id(entity))
    except Exception:
        return False
    if dialog_peer_ids is not None:
        return pid in dialog_peer_ids or int(getattr(entity, "id", 0) or 0) in dialog_peer_ids
    try:
        await client.get_permissions(entity, "me")
        return True
    except Exception:
        return False


async def _dialog_peer_ids(client: TelegramClient) -> set[int]:
    ids: set[int] = set()
    async for dialog in client.iter_dialogs():
        try:
            ids.add(int(peer_id(dialog.entity)))
        except Exception:
            pass
        try:
            eid = int(getattr(dialog.entity, "id", 0) or 0)
            if eid:
                ids.add(eid)
        except Exception:
            pass
    return ids


async def check_membership(client: TelegramClient, chats: list[Chat]) -> MembershipReport:
    report = MembershipReport()
    dialog_ids = await _dialog_peer_ids(client)
    for chat in chats:
        try:
            member = await is_member(client, chat, dialog_peer_ids=dialog_ids)
        except Exception:
            member = False
        if member:
            report.joined.append(chat)
            continue
        target = chat_join_target(chat)
        cfg = effective_join_config(chat)
        has_way = (
            target["method"] not in {"manual"}
            or bool(cfg["garant_bot"])
            or bool((chat.invite_link or "").strip())
            or bool((chat.username or "").strip())
        )
        if not has_way:
            report.no_link.append(chat)
        else:
            report.missing.append(chat)
        await asyncio.sleep(0.05)
    return report


async def _join_invite(client: TelegramClient, invite_hash: str) -> Any:
    try:
        return await client(functions.messages.ImportChatInviteRequest(invite_hash))
    except UserAlreadyParticipantError:
        return await client(functions.messages.CheckChatInviteRequest(invite_hash))


def _entity_from_join_result(result: Any) -> Any:
    if result is None:
        return None
    chats = getattr(result, "chats", None)
    if isinstance(chats, (list, tuple)) and chats:
        return chats[0]
    chat = getattr(result, "chat", None)
    if chat is not None:
        return chat
    return None


async def _join_username(client: TelegramClient, username: str) -> Any:
    entity = await client.get_entity(username)
    if isinstance(entity, types.Channel):
        try:
            return await client(functions.channels.JoinChannelRequest(entity))
        except UserAlreadyParticipantError:
            return entity
    return entity


async def _vote_poll(client: TelegramClient, message: Any) -> str | None:
    poll = getattr(message, "poll", None)
    if poll is None:
        media = getattr(message, "media", None)
        poll = getattr(media, "poll", None) if media is not None else None
    solved = solve_poll_captcha(poll)
    if solved is None:
        return None
    idx, answer = solved
    answers = getattr(poll, "answers", None) or []
    if idx < 0 or idx >= len(answers):
        return None
    option = answers[idx].option
    try:
        peer = await message.get_input_chat()
    except Exception:
        peer = message.peer_id
    try:
        await client(
            functions.messages.SendVoteRequest(
                peer=peer,
                msg_id=message.id,
                options=[option],
            )
        )
        return f"poll→{answer}"
    except Exception:
        try:
            await message.click(idx)
            return f"poll_click→{answer}"
        except Exception:
            return None


async def _click_verify(client: TelegramClient, message: Any) -> str | None:
    text = getattr(message, "message", None) or ""
    picked = pick_verify_button(message, math_answer=solve_math_captcha(text))
    if picked is None:
        return None
    row, col, label = picked
    try:
        await message.click(i=row, j=col)
        return f"btn:{label[:40]}"
    except Exception:
        pass
    try:
        await message.click(text=label)
        return f"btn:{label[:40]}"
    except Exception:
        pass
    buttons = extract_inline_buttons(message)
    for ri, ci, lab, btn in buttons:
        if ri == row and ci == col:
            data = getattr(btn, "data", None)
            if data is None:
                break
            try:
                try:
                    peer = await message.get_input_chat()
                except Exception:
                    peer = message.peer_id
                await client(
                    functions.messages.GetBotCallbackAnswerRequest(
                        peer=peer,
                        msg_id=message.id,
                        data=data,
                    )
                )
                return f"btn:{label[:40]}"
            except Exception:
                break
    return None


async def _reply_math(client: TelegramClient, message: Any, entity: Any) -> str | None:
    text = getattr(message, "message", None) or ""
    if getattr(message, "poll", None) is not None:
        return None
    if extract_inline_buttons(message):
        return None
    answer = solve_math_captcha(text)
    if answer is None:
        return None
    try:
        await client.send_message(entity, answer, reply_to=message.id)
        return f"text→{answer}"
    except Exception:
        try:
            await client.send_message(entity, answer)
            return f"text→{answer}"
        except Exception:
            return None


async def _try_solve_message(
    client: TelegramClient,
    message: Any,
    entity: Any,
    *,
    captcha_kind: str = "auto",
) -> str | None:
    if message is None:
        return None
    kind = (captcha_kind or "auto").lower()
    if kind == "none":
        return None

    order: list[str]
    if kind == "poll":
        order = ["poll", "button", "text"]
    elif kind == "verify_btn":
        order = ["button", "poll", "text"]
    elif kind == "math_btn":
        order = ["button", "poll", "text"]
    else:
        order = ["poll", "button", "text"]

    for step in order:
        if step == "poll":
            result = await _vote_poll(client, message)
        elif step == "button":
            result = await _click_verify(client, message)
        else:
            result = await _reply_math(client, message, entity)
        if result:
            return result
    return None


def _message_looks_relevant(message: Any) -> bool:
    if message is None:
        return False
    if getattr(message, "poll", None) is not None:
        return True
    media = getattr(message, "media", None)
    if media is not None and getattr(media, "poll", None) is not None:
        return True
    if extract_inline_buttons(message):
        text = getattr(message, "message", None) or ""
        if looks_like_captcha(text) or pick_verify_button(message):
            return True
        if len(extract_inline_buttons(message)) <= 4:
            return True
    text = getattr(message, "message", None) or ""
    return looks_like_captcha(text)


async def _maybe_solve_captcha(
    client: TelegramClient,
    entity: Any,
    *,
    wait_sec: float = 12.0,
    captcha_kind: str = "auto",
) -> str | None:
    if entity is None or (captcha_kind or "").lower() == "none":
        return None
    solved: list[str] = []

    async def _handler(event: events.NewMessage.Event) -> None:
        if solved:
            return
        msg = event.message
        if not _message_looks_relevant(msg):
            return
        detail = await _try_solve_message(
            client, msg, entity, captcha_kind=captcha_kind
        )
        if detail:
            solved.append(detail)

    client.add_event_handler(_handler, events.NewMessage(chats=entity))
    try:
        async for msg in client.iter_messages(entity, limit=15):
            if not _message_looks_relevant(msg):
                continue
            detail = await _try_solve_message(
                client, msg, entity, captcha_kind=captcha_kind
            )
            if detail:
                solved.append(detail)
                break
        if not solved:
            await asyncio.sleep(wait_sec)
        else:
            await asyncio.sleep(2.0)
            async for msg in client.iter_messages(entity, limit=5):
                if not _message_looks_relevant(msg):
                    continue
                detail = await _try_solve_message(
                    client, msg, entity, captcha_kind=captcha_kind
                )
                if detail and detail not in solved:
                    solved.append(detail)
                    break
    finally:
        try:
            client.remove_event_handler(_handler, events.NewMessage)
        except Exception:
            try:
                client.remove_event_handler(_handler)
            except Exception:
                pass
    return " | ".join(solved) if solved else None


def sort_chats_for_join(chats: list[Chat]) -> list[Chat]:
    """Обычные сначала, заявки (request) — в конце, чтобы не ловить FloodWait рано."""

    def key(c: Chat) -> tuple[int, str]:
        cfg = effective_join_config(c)
        late = 1 if (cfg["is_join_request"] or cfg["join_mode"] == "request") else 0
        return (late, (c.display_name or "").casefold())

    return sorted(chats, key=key)


async def join_one(
    client: TelegramClient,
    chat: Chat,
    *,
    solve_captcha: bool = True,
) -> JoinResult:
    title = chat.display_name
    cfg = effective_join_config(chat)
    target = chat_join_target(chat)
    details: list[str] = []

    if target["method"] == "manual":
        return JoinResult(
            title,
            chat.id,
            "needs_manual",
            target.get("reason") or "нужно вступить вручную",
        )

    try:
        if await is_member(client, chat):
            # всё равно можем добить after_join / каналы
            entity = await lookup_entity(client, chat)
            if cfg["require_channels"]:
                notes = await subscribe_required(client, cfg["require_channels"])
                if notes:
                    details.append("ch:" + ",".join(notes[:4]))
            return JoinResult(title, chat.id, "already", "уже в чате" + (f" | {' | '.join(details)}" if details else ""))
    except Exception:
        pass

    entity = None
    status = "joined"

    try:
        if target["method"] == "garant":
            invite_url = await find_invite_from_bot(client, target["value"])
            if not invite_url:
                return JoinResult(
                    title, chat.id, "failed", f"бот @{target['value']}: ссылка не найдена"
                )
            details.append(f"garant:{invite_url[:32]}")
            inv = extract_invite_hash(invite_url)
            if not inv:
                return JoinResult(title, chat.id, "failed", "бот вернул не-invite ссылку")
            st, entity = await join_via_invite_or_request(
                client, inv, as_request=cfg["is_join_request"] or cfg["join_mode"] == "request"
            )
            status = st
            if st == "request_sent":
                details.append("заявка отправлена")
                return JoinResult(title, chat.id, "request_sent", " | ".join(details))

        elif target["method"] == "folder":
            folder_result = await join_folder(
                client,
                target["value"],
                solve_captcha_fn=(
                    (lambda c, e: _maybe_solve_captcha(c, e, captcha_kind=cfg["captcha_kind"]))
                    if solve_captcha
                    else None
                ),
            )
            if not folder_result.get("ok"):
                return JoinResult(
                    title, chat.id, "failed", folder_result.get("error") or "folder fail"
                )
            caps = folder_result.get("captchas") or []
            details.append(f"folder:{folder_result.get('joined', 0)}")
            if caps:
                details.extend(caps[:3])
                status = "captcha_ok"
            return JoinResult(title, chat.id, status, " | ".join(details))

        elif target["method"] == "invite":
            st, entity = await join_via_invite_or_request(
                client,
                target["value"],
                as_request=cfg["is_join_request"] or cfg["join_mode"] == "request",
            )
            status = st
            if st == "request_sent":
                return JoinResult(title, chat.id, "request_sent", "заявка отправлена")

        elif target["method"] == "username":
            entity = await _join_username(client, target["value"])
        else:
            entity = await client.get_entity(
                int(target["value"]) if target["value"].lstrip("-").isdigit() else target["value"]
            )
            if isinstance(entity, types.Channel):
                try:
                    await client(functions.channels.JoinChannelRequest(entity))
                except UserAlreadyParticipantError:
                    status = "already"
    except UserAlreadyParticipantError:
        status = "already"
    except (InviteHashExpiredError, InviteHashInvalidError) as e:
        return JoinResult(title, chat.id, "failed", f"инвайт недействителен: {e}")
    except FloodWaitError as e:
        wait = min(int(getattr(e, "seconds", 1) or 1) + 1, 120)
        await asyncio.sleep(wait)
        return JoinResult(title, chat.id, "failed", f"FloodWait {getattr(e, 'seconds', '?')}s")
    except Exception as e:
        return JoinResult(title, chat.id, "failed", f"{type(e).__name__}: {e}")

    if entity is None:
        try:
            entity = await lookup_entity(client, chat)
        except Exception:
            entity = None

    # обязательные каналы (MARKET 404 и т.п.)
    if cfg["require_channels"]:
        notes = await subscribe_required(client, cfg["require_channels"])
        if notes:
            details.append("ch:" + ",".join(notes[:4]))

    # after_join: confirm / bot_captcha
    after = cfg["after_join"]
    after_bot = cfg["after_join_bot"] or cfg["garant_bot"]
    if after == "confirm" and after_bot:
        hit = await click_confirm_in_bot(client, after_bot)
        if hit:
            details.append(f"confirm:{hit}")
            status = "captcha_ok"
        else:
            details.append("confirm:не найдена кнопка")
    elif after == "bot_captcha" and after_bot:
        hit = await solve_bot_math_captcha(client, after_bot)
        if hit:
            details.append(hit)
            status = "captcha_ok"
        else:
            details.append("bot_captcha:не решена")

    # капча в самом чате
    if solve_captcha and entity is not None and cfg["captcha_kind"] != "none":
        await asyncio.sleep(1.2)
        try:
            answer = await _maybe_solve_captcha(
                client, entity, captcha_kind=cfg["captcha_kind"]
            )
        except Exception as e:
            answer = None
            details.append(f"капча:{type(e).__name__}")
        if answer:
            status = "captcha_ok"
            details.append(f"капча {answer}")

    if entity is not None:
        try:
            cid = str(peer_id(entity))
            if cid and cid != chat.chat_id:
                details.append(f"id={cid}")
        except Exception:
            pass

    return JoinResult(title, chat.id, status, " | ".join(details))


async def join_many(
    client: TelegramClient,
    chats: list[Chat],
    *,
    solve_captcha: bool = True,
    progress=None,
) -> list[JoinResult]:
    ordered = sort_chats_for_join(chats)
    results: list[JoinResult] = []
    total = len(ordered)
    for i, chat in enumerate(ordered, 1):
        result = await join_one(client, chat, solve_captcha=solve_captcha)
        results.append(result)
        if progress:
            await progress(i, total, result)
        # заявки — больше пауза
        cfg = effective_join_config(chat)
        delay = 2.5 if (cfg["is_join_request"] or cfg["join_mode"] == "request") else 0.7
        await asyncio.sleep(delay)
    return results
