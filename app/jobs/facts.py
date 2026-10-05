"""Факты отправки: что аккаунты реально написали в чаты (по истории чата).

Планировщик Telegram / Autoposter мог не отправить сообщение (бан, мут, удалили,
сбой пересборки) — настройка («setup ok») этого не показывает. Поэтому раз в час
читаем собственные сообщения аккаунтов в чатах каталога и складываем в БД.
Из этих же данных строится таблица за любой час / любые сутки.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from telethon import TelegramClient

from app.config import get_settings
from app.jobs import LogSink, runtime
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.jobs.setup import member_schedule_chats
from app.models import Account, Chat
from app.notify import safe_send
from app.store import Store
from app.tg.client import peer_id, telethon_client
from app.tg.restrictions import scan_account
from app.tg.scheduler import fetch_scheduled
from app.utils.balance import PairFact
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
    scan_dms: bool = False,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "new": 0,
        "chats": 0,
        "not_member": 0,
        "ban": 0,
        "mute": 0,
        "resolved": 0,
        "resolved_chats": [],
        "dm_new": 0,
        "dm_pending": 0,
    }
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
            stats["resolved_chats"] = list(res.get("resolved_chats") or [])
        if scan_dms:
            from app.jobs.dms import collect_dms

            dm = await collect_dms(store, client, account)
            stats["dm_new"] = dm.new_people
            stats["dm_pending"] = dm.pending
    return stats


async def _setup_after_mute_resolve(
    store: Store,
    account: Account,
    chat_pks: list[int],
    bot: Bot | None,
    admin_chat_id: int | None,
) -> None:
    """Мут/nowrite сняли → сразу пересобираем schedule, не ждём weekly/retry."""
    if not chat_pks:
        return
    from app.jobs.setup import run_setup_chats_only

    try:
        await run_setup_chats_only(
            store,
            account.id,
            chat_pks,
            bot,
            admin_chat_id,
            job_kind="setup_after_unmute",
            skip_abandoned=False,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "setup_after_unmute failed for %s chats=%s", account.label, chat_pks
        )


async def run_facts_collection(
    store: Store,
    *,
    hours: float = 3,
    scan_restrictions: bool = True,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    account_ids: list[int] | None = None,
    scan_dms: bool = True,
    quiet: bool = False,
) -> str:
    """Собрать факты отправки за последние `hours` часов по всем аккаунтам.

    quiet=True — плановый сбор: итог в чат не шлём (только при ошибках аккаунтов).
    """
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

    async def _one(acc: Account) -> dict[str, Any]:
        acc_chats = [c for c in chats if (acc.id, c.id) not in banned]
        try:
            res = await collect_account(
                store,
                acc,
                acc_chats,
                since,
                scan_restrictions=scan_restrictions,
                scan_dms=scan_dms,
            )
        except Exception as e:  # noqa: BLE001
            await store.set_setting(
                f"facts_status:{acc.id}",
                f"err|{to_iso(datetime.now(timezone.utc))}|{type(e).__name__}: {e}"[:300],
            )
            raise
        await store.set_setting(
            f"facts_status:{acc.id}", f"ok|{to_iso(datetime.now(timezone.utc))}|"
        )
        return res

    results = await map_batches(accounts, _one, batch_size=batch_size, batch_pause=batch_pause)
    total = {
        "new": 0,
        "chats": 0,
        "not_member": 0,
        "ban": 0,
        "mute": 0,
        "resolved": 0,
        "dm_new": 0,
        "dm_pending": 0,
    }
    failed = 0
    failed_names: list[str] = []
    for acc, res in zip(accounts, results):
        if isinstance(res, BaseException):
            failed += 1
            failed_names.append(f"{acc.label} ({type(res).__name__})")
            log.warning("facts failed for %s: %s", acc.label, res)
            continue
        for k, v in res.items():
            if k == "resolved_chats":
                continue
            total[k] += v
        resolved_chats = list(res.get("resolved_chats") or [])
        if resolved_chats:
            await _setup_after_mute_resolve(
                store, acc, resolved_chats, bot, admin_chat_id
            )
    await store.prune_send_events(90)
    took = time.monotonic() - started
    summary = (
        f"Факты за {hours:g} ч: аккаунтов {len(accounts) - failed}/{len(accounts)}, "
        f"новых отправок {total['new']}, пар «аккаунт×чат» в чате {total['chats']}, "
        f"не состоит {total['not_member']}"
    )
    if scan_restrictions:
        summary += f" | баны {total['ban']}, муты {total['mute']}, снято {total['resolved']}"
    if scan_dms:
        summary += f" | новых людей в ЛС {total['dm_new']}"
        if total["dm_pending"]:
            summary += f" (в очереди индексации {total['dm_pending']} диалогов)"
    summary += f" | {took:.0f} с"
    if failed_names:
        summary += "\nНе удалось прочитать: " + ", ".join(failed_names[:10])
    if bot and admin_chat_id and (not quiet or failed_names):
        await safe_send(bot, admin_chat_id, summary)
    log.info(summary)
    # Самолечение: после фактов в фоне перераспределить слоты (если дыры).
    # collect=True — PairFact для balance; notify=changes — человеку только если что-то чинили.
    try:
        from app.config import get_settings as _gs
        from app.jobs.balance import run_smart_rebalance

        if _gs().auto_rebalance and not runtime.is_running("smart_rebalance", 0):

            async def _reb() -> None:
                try:
                    await run_smart_rebalance(
                        store, bot, admin_chat_id, collect=True, notify="changes"
                    )
                except Exception:  # noqa: BLE001
                    log.exception("post-facts auto rebalance failed")

            try:
                runtime.spawn("smart_rebalance", 0, _reb())
            except RuntimeError:
                pass
    except Exception:  # noqa: BLE001
        log.exception("post-facts auto rebalance schedule failed")
    return summary


def spawn_facts(store: Store, **kwargs: Any):
    """Запуск в фоне без наложения двух сборов."""
    if runtime.is_running("facts", 0):
        return None
    return runtime.spawn("facts", 0, run_facts_collection(store, **kwargs))


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
        if not member:
            ok_states = await store.list_setup_states(account.id, statuses=["ok"])
            if len(ok_states) >= 3:
                # пустой результат при многих «ok» — скорее сбой выдачи диалогов, чем выход из всех чатов
                raise RuntimeError("список диалогов не содержит ни одного чата каталога — не доверяю")
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
            sched: int | None
            try:
                sched = len(await fetch_scheduled(client, ent))
            except Exception as e:
                sched, err3 = None, f"{type(e).__name__}: {e}"
            else:
                err3 = ""
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
                    error="; ".join(e for e in (err1, err2, err3) if e),
                    checked_at=checked,
                    scheduled_count=sched,
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
