"""Клоакинг для аутрич-аккаунтов: ответ в ЛС через Telethon (не Autoposter/sender).

Условия отправки: человек написал первым, не админ ни в одном чате каталога, где
мы состоим, текст клоакинга включён. Один раз на человека.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import ChannelParticipantsAdmins

from app.models import Account, Chat
from app.store import Store
from app.tg.sender_push import normalize_multiline
from app.utils.entities import entities_loads, to_telethon_entities
from app.utils.errfmt import short_error
from app.utils.timefmt import to_iso

log = logging.getLogger("marketing.outreach_cloak")

PER_ACCOUNT_BUDGET = 20
PAUSE_SEC = 0.35
RECENT_HOURS = 72


@dataclass
class CloakAccountResult:
    label: str
    sent: int = 0
    skipped_admin: int = 0
    skipped_already: int = 0
    errors: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


async def collect_admin_ids(client: Any, entities: list[Any]) -> set[int]:
    """user_id админов/создателей во всех чатах, где аккаунт состоит."""
    ids: set[int] = set()
    for ent in entities:
        try:
            async for user in client.iter_participants(
                ent, filter=ChannelParticipantsAdmins()
            ):
                uid = int(getattr(user, "id", 0) or 0)
                if uid:
                    ids.add(uid)
        except Exception as e:  # noqa: BLE001
            log.debug("admins list failed for %s: %s", getattr(ent, "id", "?"), e)
    return ids


async def _already_sent_cloak(client: Any, entity: Any, cloak_norm: str) -> bool:
    if not cloak_norm:
        return False
    try:
        async for msg in client.iter_messages(entity, limit=30):
            if not getattr(msg, "out", False):
                continue
            if _norm(getattr(msg, "message", "") or "") == cloak_norm:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def _peer_is_admin_fallback(
    client: Any, entities: list[Any], user_id: int
) -> bool:
    """Если список админов чата недоступен — точечная проверка прав."""
    for ent in entities:
        try:
            perms = await client.get_permissions(ent, user_id)
        except Exception:  # noqa: BLE001
            continue
        if getattr(perms, "is_admin", False) or getattr(perms, "is_creator", False):
            return True
    return False


async def process_outreach_cloak(
    store: Store,
    client: Any,
    account: Account,
    chats: list[Chat],
    *,
    cloak_text: str,
    cloak_entities_json: str = "[]",
    budget: int = PER_ACCOUNT_BUDGET,
    admin_ids: set[int] | None = None,
    chat_entities: list[Any] | None = None,
) -> CloakAccountResult:
    """Отправить клоакинг ожидающим людям для одного аутрич-аккаунта."""
    res = CloakAccountResult(label=account.label)
    text = normalize_multiline(cloak_text)
    if not account.is_outreach or not text.strip():
        return res

    since = to_iso(datetime.now(timezone.utc) - timedelta(hours=RECENT_HOURS))
    pending = await store.list_dm_cloak_pending(
        account.id, limit=budget, since=since
    )
    if not pending:
        return res

    entities = list(chat_entities or [])
    if admin_ids is None:
        admin_ids = await collect_admin_ids(client, entities) if entities else set()

    tl_ents = to_telethon_entities(entities_loads(cloak_entities_json)) or None
    cloak_norm = _norm(text)
    now = to_iso(datetime.now(timezone.utc))

    for person in pending:
        uid = int(person["user_id"])
        try:
            # Список админов собрали по чатам каталога; если пуст — точечная проверка.
            is_admin = uid in admin_ids
            if not is_admin and not admin_ids and entities:
                is_admin = await _peer_is_admin_fallback(client, entities, uid)
            if is_admin:
                await store.set_dm_cloak_state(account.id, uid, "admin", at=now)
                res.skipped_admin += 1
                continue

            try:
                entity = await client.get_entity(uid)
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"{uid}: {short_error(e)}")
                await store.set_dm_cloak_state(account.id, uid, "error", at=now)
                continue

            if await _already_sent_cloak(client, entity, cloak_norm):
                await store.set_dm_cloak_state(account.id, uid, "sent", at=now)
                res.skipped_already += 1
                continue

            send_kw: dict[str, Any] = {}
            if tl_ents:
                send_kw["formatting_entities"] = tl_ents
            while True:
                try:
                    await client.send_message(entity, text, **send_kw)
                    break
                except FloodWaitError as e:
                    await asyncio.sleep(min(int(getattr(e, "seconds", 5) or 5), 120))
                except RPCError as e:
                    raise RuntimeError(short_error(e)) from e

            await store.set_dm_cloak_state(account.id, uid, "sent", at=now)
            res.sent += 1
            await asyncio.sleep(PAUSE_SEC)
        except Exception as e:  # noqa: BLE001
            err = short_error(e)
            res.errors.append(f"{uid}: {err}")
            log.info("outreach cloak %s → %s: %s", account.label, uid, err)
            await store.set_dm_cloak_state(account.id, uid, "error", at=now)

    return res
