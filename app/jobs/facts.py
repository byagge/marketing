"""Сбор фактов по парам «аккаунт × чат»: состоит ли, можно ли писать, сколько
реально отправлено за сутки. На этих фактах строится перераспределение слотов
и экспорт отчёта — план из БД («назначено») часто расходится с реальностью."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from telethon import TelegramClient

from app.config import get_settings
from app.jobs import LogSink, runtime
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.jobs.setup import member_schedule_chats
from app.models import Account, Chat
from app.store import Store
from app.tg.client import telethon_client
from app.utils.balance import PairFact

log = logging.getLogger("marketing.facts")


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _until(rights: Any) -> tuple[bool, str]:
    """(запрещено ли писать сейчас, до какого момента)."""
    if rights is None or not getattr(rights, "send_messages", False):
        return False, ""
    until = getattr(rights, "until_date", None)
    now = datetime.now(timezone.utc)
    if until is None:
        return True, "forever"
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    # до 1970/понадобится ≥366 дней → Telegram трактует как навсегда
    if until.year <= 1971 or until - now > timedelta(days=366):
        return True, "forever"
    if until > now:
        return True, _iso(until)
    return False, ""


async def probe_can_send(client: TelegramClient, entity: Any) -> tuple[int | None, str, str]:
    """(can_send 1/0/None, mute_until, error)."""
    try:
        perms = await client.get_permissions(entity, "me")
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"
    if perms is None:
        return None, "", ""
    if getattr(perms, "has_left", False):
        return 0, "", "left"
    if getattr(perms, "is_admin", False) or getattr(perms, "is_creator", False):
        return 1, "", ""
    participant = getattr(perms, "participant", None)
    blocked, until = _until(getattr(participant, "banned_rights", None))
    if blocked:
        return 0, until, ""
    # общий запрет «писать могут только админы» на весь чат
    blocked, until = _until(getattr(entity, "default_banned_rights", None))
    if blocked and getattr(perms, "has_default_permissions", True):
        return 0, "chat_locked", ""
    return 1, "", ""


async def probe_sent(
    client: TelegramClient, entity: Any, since: datetime
) -> tuple[int | None, str, str]:
    """(отправлено своих сообщений за период, время последнего, error)."""
    count = 0
    last: datetime | None = None
    try:
        async for msg in client.iter_messages(entity, from_user="me", limit=300):
            date = msg.date
            if date is None:
                continue
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if date < since:
                break
            count += 1
            if last is None:
                last = date
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"
    return count, _iso(last), ""


async def collect_account_facts(
    store: Store, account: Account, chats: list[Chat]
) -> list[PairFact]:
    settings = get_settings()
    pause = float(settings.facts_chat_pause_sec)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    checked = _iso(now)
    facts: list[PairFact] = []
    async with telethon_client(account.telethon_session) as client:
        me = await client.get_me()
        await store.update_account(
            account.id,
            is_premium=1 if getattr(me, "premium", False) else 0,
            username=getattr(me, "username", None) or None,
        )
        member = await member_schedule_chats(client, chats)
        for chat in chats:
            ent = member.get(chat.id)
            if ent is None:
                facts.append(
                    PairFact(account.id, chat.id, member=0, can_send=0, checked_at=checked)
                )
                continue
            can_send, mute_until, err1 = await probe_can_send(client, ent)
            await asyncio.sleep(pause)
            sent, last, err2 = await probe_sent(client, ent, since)
            await asyncio.sleep(pause)
            facts.append(
                PairFact(
                    account.id,
                    chat.id,
                    member=1,
                    can_send=can_send,
                    mute_until=mute_until,
                    sent_24h=sent,
                    last_sent_at=last,
                    error="; ".join(e for e in (err1, err2) if e),
                    checked_at=checked,
                )
            )
    return facts


async def run_collect_facts(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    only_account_ids: set[int] | None = None,
    job_kind: str = "collect_facts",
) -> dict[str, int]:
    """Обновить chat_facts по всем (или выбранным) аккаунтам. Мёртвые тоже — для отчёта."""
    chats = [c for c in await store.list_chats(enabled_only=True) if c.is_schedule]
    accounts = [
        a
        for a in await store.list_accounts()
        if a.telethon_session and (only_account_ids is None or a.id in only_account_ids)
    ]
    stats = {"accounts": len(accounts), "ok": 0, "failed": 0, "pairs": 0}
    if not chats or not accounts:
        return stats
    job = await store.create_job(job_kind, None)
    sink = LogSink(store, job.id, None, None)
    batch_size, batch_pause = setup_parallel_defaults()

    async def _one(acc: Account) -> bool:
        if runtime.cancelled(job_kind, 0):
            return False
        try:
            facts = await collect_account_facts(store, acc, chats)
        except Exception as e:
            # сессия не читается и т.п.: старые факты не трогаем (станут UNKNOWN по возрасту)
            await sink.emit(f"✗ {acc.label}: {type(e).__name__}: {e}", "error", notify=False)
            return False
        for f in facts:
            await store.upsert_fact(f)
        stats["pairs"] += len(facts)
        await sink.emit(f"✓ {acc.label}: {len(facts)} пар", notify=False)
        return True

    results = await map_batches(
        accounts,
        _one,
        batch_size=batch_size,
        batch_pause=batch_pause,
        is_cancelled=lambda: runtime.cancelled(job_kind, 0),
    )
    stats["ok"] = sum(1 for r in results if r is True)
    stats["failed"] = len(results) - stats["ok"]
    await store.finish_job(
        job.id,
        "done",
        f"аккаунтов {stats['ok']}/{stats['accounts']}, пар {stats['pairs']}, "
        f"ошибок {stats['failed']}",
    )
    return stats
