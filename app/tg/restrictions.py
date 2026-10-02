"""Баны, муты и запрет писать: определение, причина мута, сохранение в БД.

Бан  — аккаунт исключён из чата (в чаты, где бан, больше не вступаем и не пишем).
Мут  — аккаунт в чате, но писать нельзя (до даты) — это НЕ бан; сохраняем срок и
       причину (ближайшее сообщение с отметкой аккаунта, обычно от бота).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    UserNotParticipantError,
)
from telethon.tl import functions, types

from app.models import Account, Chat
from app.store import Store
from app.utils.timefmt import to_iso
from app.utils.tg_links import format_message_link

log = logging.getLogger("marketing.restrictions")

REASON_MAX = 600


@dataclass
class Probe:
    # ok | ban | mute | nowrite | left | unknown
    kind: str
    until: datetime | None = None
    detail: str = ""


def classify_text(error: str) -> str | None:
    """По тексту ошибки: ban | mute | None."""
    low = (error or "").casefold().replace(" ", "").replace("_", "")
    if "userbannedinchannel" in low or "bannedfromsendingmessages" in low:
        return "ban"
    if "chatwriteforbidden" in low or "can'twriteinthischat" in low or "can’twriteinthischat" in low:
        return "mute"
    return None


def classify_exception(exc: BaseException) -> str | None:
    if isinstance(exc, UserBannedInChannelError):
        return "ban"
    if isinstance(exc, ChatWriteForbiddenError):
        return "mute"
    return classify_text(f"{type(exc).__name__}: {exc}")


def _until_or_none(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        # Telegram: <30 сек или >366 дн. = навсегда
        if dt.year <= 1971 or dt > now + timedelta(days=366):
            return None
        return dt
    return None


async def probe_restriction(client: TelegramClient, entity: Any) -> Probe:
    """Что именно ограничивает аккаунт в этом чате."""
    if isinstance(entity, types.Channel):
        try:
            res = await client(
                functions.channels.GetParticipantRequest(entity, types.InputPeerSelf())
            )
        except UserNotParticipantError:
            return Probe("left", detail="не участник")
        except ChannelPrivateError:
            return Probe("left", detail="чат недоступен (приватный / нет в аккаунте)")
        except Exception as e:  # noqa: BLE001
            return Probe("unknown", detail=f"{type(e).__name__}: {e}")
        part = res.participant
        if isinstance(part, types.ChannelParticipantBanned):
            rights = part.banned_rights
            until = _until_or_none(getattr(rights, "until_date", None))
            if getattr(rights, "view_messages", False) or getattr(part, "left", False):
                return Probe("ban", until, "исключён из чата")
            if any(
                getattr(rights, name, False)
                for name in ("send_messages", "send_plain", "send_media", "send_photos")
            ):
                return Probe("mute", until, "ограничен администратором")
            return Probe("ok")
        if isinstance(part, types.ChannelParticipantLeft):
            return Probe("left", detail="не участник")
        if isinstance(
            part, (types.ChannelParticipantAdmin, types.ChannelParticipantCreator)
        ):
            return Probe("ok")
        default = getattr(entity, "default_banned_rights", None)
        if getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
            return Probe("nowrite", detail="это канал: писать могут только админы")
        if default is not None and (
            getattr(default, "send_messages", False) or getattr(default, "send_plain", False)
        ):
            return Probe("nowrite", detail="в чате запрещено писать всем участникам")
        return Probe("ok")

    default = getattr(entity, "default_banned_rights", None)
    if getattr(entity, "left", False):
        return Probe("left", detail="не участник")
    if default is not None and getattr(default, "send_messages", False):
        return Probe("nowrite", detail="в чате запрещено писать всем участникам")
    return Probe("ok")


async def find_mute_reason(
    client: TelegramClient, entity: Any
) -> tuple[str, str]:
    """Ближайшее сообщение в чате с отметкой этого аккаунта (обычно бот: причина мута)."""
    best = None
    try:
        async for msg in client.iter_messages(
            entity, limit=15, filter=types.InputMessagesFilterMyMentions()
        ):
            sender = None
            try:
                sender = await msg.get_sender()
            except Exception:  # noqa: BLE001
                pass
            is_bot = bool(getattr(sender, "bot", False))
            if best is None:
                best = msg
            if is_bot:
                best = msg
                break
    except Exception as e:  # noqa: BLE001
        log.info("mute reason lookup failed: %s", e)
        return "", ""
    if best is None:
        return "", ""
    text = (getattr(best, "message", "") or "").strip()
    if not text:
        text = "(сообщение без текста)"
    username = (getattr(entity, "username", None) or "").strip()
    if username:
        link = format_message_link(username, best.id)
    else:
        try:
            link = format_message_link(int(f"-100{entity.id}"), best.id)
        except Exception:  # noqa: BLE001
            link = ""
    when = getattr(best, "date", None)
    stamp = ""
    if isinstance(when, datetime):
        stamp = when.astimezone(timezone.utc).strftime("[%d.%m %H:%M UTC] ")
    return (stamp + text)[:REASON_MAX], link


async def record_restriction(
    store: Store,
    client: TelegramClient | None,
    account: Account,
    chat: Chat,
    entity: Any,
    *,
    hint: str | None,
    error: str = "",
    probe: Probe | None = None,
) -> str | None:
    """
    Сохранить бан/мут для пары. hint — что подсказала ошибка (ban|mute).
    Возвращает итоговый kind или None, если ограничения нет.
    """
    if probe is None and client is not None and entity is not None:
        try:
            probe = await probe_restriction(client, entity)
        except Exception as e:  # noqa: BLE001
            log.info("probe failed: %s", e)
            probe = None

    kind: str | None = None
    until: datetime | None = None
    detail = ""
    if probe is not None and probe.kind in {"ban", "mute", "nowrite"}:
        kind, until, detail = probe.kind, probe.until, probe.detail
    elif hint == "ban" and probe is not None and probe.kind == "ok":
        # участник без ограничений, но писать нельзя → это лимит самого аккаунта
        # (@SpamBot), а не бан в чате: в базу банов не пишем
        return None
    elif hint == "ban":
        kind = "ban"
    elif hint == "mute":
        kind = "mute"
    if kind is None:
        return None

    reason = link = ""
    if kind in {"mute", "nowrite"} and client is not None and entity is not None:
        existing = await store.get_restriction(account.id, chat.id)
        if existing and existing.is_mute and existing.reason:
            reason, link = existing.reason, existing.reason_link
        else:
            reason, link = await find_mute_reason(client, entity)
    if not reason and detail:
        reason = detail
    await store.upsert_restriction(
        account.id,
        chat.id,
        kind,
        reason=reason,
        reason_link=link,
        error=error,
        until_at=to_iso(until) if until else "",
    )
    return kind


async def scan_account(
    store: Store,
    client: TelegramClient,
    account: Account,
    chats: list[Chat],
    entities: dict[int, Any],
) -> dict[str, int]:
    """Проверить все чаты каталога: найти баны/муты и снять закончившиеся."""
    stats = {"ban": 0, "mute": 0, "nowrite": 0, "resolved": 0, "ok": 0}
    for chat in chats:
        entity = entities.get(chat.id)
        if entity is None:
            continue
        try:
            probe = await probe_restriction(client, entity)
        except Exception:  # noqa: BLE001
            continue
        if probe.kind in {"ban", "mute", "nowrite"}:
            await record_restriction(
                store, client, account, chat, entity, hint=None, probe=probe
            )
            stats[probe.kind] += 1
        elif probe.kind == "ok":
            stats["ok"] += 1
            if await store.resolve_restriction(account.id, chat.id):
                stats["resolved"] += 1
                # ограничение снято — пару можно снова настраивать
                await store.reset_setup_state(account.id, chat.id)
    return stats
