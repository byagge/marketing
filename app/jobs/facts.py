"""Факты отправки: что аккаунты реально написали в чаты (по истории чата).

Планировщик Telegram / Autoposter мог не отправить сообщение (бан, мут, удалили,
сбой пересборки) — настройка («setup ok») этого не показывает. Поэтому раз в час
читаем собственные сообщения аккаунтов в чатах каталога и складываем в БД.
Из этих же данных строится таблица за любой час / любые сутки.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from telethon import TelegramClient

from app.jobs import runtime
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.models import Account, Chat
from app.notify import safe_send
from app.store import Store
from app.tg.client import peer_id, telethon_client
from app.tg.restrictions import scan_account
from app.utils.chat_ids import canon_chat_id
from app.utils.timefmt import to_iso

log = logging.getLogger("marketing.facts")

MAX_PER_CHAT = 400


async def dialog_entities(client: TelegramClient, chats: list[Chat]) -> dict[int, Any]:
    """chat.pk → entity, только чаты, где аккаунт состоит (по диалогам, 1 запрос)."""
    by_key: dict[str, Any] = {}
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        try:
            by_key[canon_chat_id(peer_id(ent))] = ent
        except Exception:  # noqa: BLE001
            pass
        eid = getattr(ent, "id", None)
        if eid:
            by_key.setdefault(canon_chat_id(eid), ent)
        uname = (getattr(ent, "username", None) or "").casefold()
        if uname:
            by_key.setdefault(uname, ent)
    found: dict[int, Any] = {}
    for chat in chats:
        ent = by_key.get(canon_chat_id(chat.chat_id))
        if ent is None and chat.username:
            ent = by_key.get(canon_chat_id(chat.username))
        if ent is not None:
            found[chat.id] = ent
    return found


async def collect_chat_sends(
    store: Store,
    client: TelegramClient,
    account: Account,
    chat: Chat,
    entity: Any,
    since: datetime,
) -> int:
    rows: list[tuple[int, int, str, int]] = []
    try:
        async for msg in client.iter_messages(entity, from_user="me", limit=MAX_PER_CHAT):
            when = getattr(msg, "date", None)
            if when is None:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < since:
                break
            rows.append((account.id, chat.id, to_iso(when), int(msg.id)))
    except Exception as e:  # noqa: BLE001
        await store.mark_scan(account.id, chat.id, "error", f"{type(e).__name__}: {e}")
        return 0
    await store.mark_scan(account.id, chat.id, "ok", "")
    return await store.add_send_events(rows)


async def collect_account(
    store: Store,
    account: Account,
    chats: list[Chat],
    since: datetime,
    *,
    scan_restrictions: bool,
) -> dict[str, int]:
    stats = {"new": 0, "chats": 0, "not_member": 0, "ban": 0, "mute": 0, "resolved": 0}
    async with telethon_client(account.telethon_session) as client:
        entities = await dialog_entities(client, chats)
        for chat in chats:
            ent = entities.get(chat.id)
            if ent is None:
                stats["not_member"] += 1
                await store.mark_scan(account.id, chat.id, "not_member", "нет в диалогах")
                continue
            stats["chats"] += 1
            stats["new"] += await collect_chat_sends(store, client, account, chat, ent, since)
        if scan_restrictions:
            res = await scan_account(store, client, account, chats, entities)
            stats["ban"] += res["ban"]
            stats["mute"] += res["mute"] + res["nowrite"]
            stats["resolved"] += res["resolved"]
    return stats


async def run_facts_collection(
    store: Store,
    *,
    hours: float = 3,
    scan_restrictions: bool = True,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    account_ids: list[int] | None = None,
) -> str:
    """Собрать факты отправки за последние `hours` часов по всем аккаунтам."""
    started = time.monotonic()
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    if account_ids is not None:
        accounts = [a for a in accounts if a.id in set(account_ids)]
    chats = [c for c in await store.list_chats(enabled_only=True)]
    if not accounts or not chats:
        return "Факты: нет аккаунтов с Telethon или чатов"

    banned = await store.banned_pairs()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    batch_size, batch_pause = setup_parallel_defaults()

    async def _one(acc: Account) -> dict[str, int]:
        acc_chats = [c for c in chats if (acc.id, c.id) not in banned]
        return await collect_account(
            store, acc, acc_chats, since, scan_restrictions=scan_restrictions
        )

    results = await map_batches(accounts, _one, batch_size=batch_size, batch_pause=batch_pause)
    total = {"new": 0, "chats": 0, "not_member": 0, "ban": 0, "mute": 0, "resolved": 0}
    failed = 0
    for acc, res in zip(accounts, results):
        if isinstance(res, BaseException):
            failed += 1
            log.warning("facts failed for %s: %s", acc.label, res)
            continue
        for k, v in res.items():
            total[k] += v
    await store.prune_send_events(90)
    took = time.monotonic() - started
    summary = (
        f"Факты за {hours:g} ч: аккаунтов {len(accounts) - failed}/{len(accounts)}, "
        f"новых отправок {total['new']}, пар «аккаунт×чат» в чате {total['chats']}, "
        f"не состоит {total['not_member']}"
    )
    if scan_restrictions:
        summary += f" | баны {total['ban']}, муты {total['mute']}, снято {total['resolved']}"
    summary += f" | {took:.0f} с"
    if bot and admin_chat_id:
        await safe_send(bot, admin_chat_id, summary)
    log.info(summary)
    return summary


def spawn_facts(store: Store, **kwargs: Any):
    """Запуск в фоне без наложения двух сборов."""
    if runtime.is_running("facts", 0):
        return None
    return runtime.spawn("facts", 0, run_facts_collection(store, **kwargs))
