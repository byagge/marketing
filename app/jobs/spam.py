"""Спамблок аккаунта: проверка @SpamBot, ступени нагрузки sender, dead-режим."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram import Bot

from app.config import get_settings
from app.jobs.parallel import map_batches
from app.models import Account, SpamState
from app.notify import safe_send
from app.sender_api import SenderAPI, SenderAPIError
from app.store import Store
from app.tg.client import telethon_client
from app.tg.spambot import check_spambot
from app.utils.spamstate import (
    CheckOutcome,
    check_due,
    compute_load,
    evaluate_check,
)
from app.utils.errfmt import short_error

log = logging.getLogger("marketing.spam")

STATUS_LABEL = {
    "clean": "чист",
    "limited": "СПАМ-ОГРАНИЧЕН",
    "unknown": "не определён",
    "": "не проверялся",
}


@dataclass
class SpamCheckResult:
    account: Account
    outcome: CheckOutcome | None = None
    skipped: str = ""
    error: str = ""
    events: list[str] = field(default_factory=list)


def _status_from_spambot(status) -> tuple[str, str, str]:
    """SpamStatus | SpamResult → (spam_status, spam_until, spam_detail) для колонок accounts."""
    # Старый SpamResult(status=...) из production-тестов
    legacy = getattr(status, "status", None)
    if legacy in {"clean", "limited", "unknown"} and not hasattr(status, "known"):
        return legacy, getattr(status, "until", "") or "", (getattr(status, "detail", "") or "")[:300]
    if not getattr(status, "known", False):
        return "unknown", "", (getattr(status, "text", "") or getattr(status, "detail", "") or "")[:300]
    if status.limited:
        until_val = status.until
        if hasattr(until_val, "isoformat"):
            until = until_val.isoformat(timespec="seconds")
        else:
            until = str(until_val or "")
        return "limited", until, (getattr(status, "text", "") or getattr(status, "detail", "") or "")[:300]
    return "clean", "", (getattr(status, "text", "") or getattr(status, "detail", "") or "")[:300]


async def check_account_spam(store: Store, account: Account) -> str:
    """Разовая проверка @SpamBot → колонки accounts.spam_* + снятие spamblock-restrictions."""
    if not account.telethon_session:
        return ""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        async with telethon_client(account.telethon_session) as client:
            res = await check_spambot(client)
    except Exception as e:  # noqa: BLE001
        await store.update_account(
            account.id,
            spam_status="unknown",
            spam_checked_at=now,
            spam_detail=f"{short_error(e)}"[:300],
        )
        return "unknown"
    status, until, detail = _status_from_spambot(res)
    await store.update_account(
        account.id,
        spam_status=status,
        spam_checked_at=now,
        spam_until=until,
        spam_detail=detail,
    )
    if status == "clean":
        await store.resolve_restrictions_kind(account.id, "spamblock")
    return status


async def account_load(
    store: Store, account: Account, *, now: datetime | None = None
) -> int:
    """Текущая нагрузка sender аккаунта в процентах (100/50/25/0) по состоянию спамблока."""
    cfg = get_settings()
    state = await store.get_spam_state(account.id)
    return compute_load(
        state,
        dead=account.sender_forbidden,
        now=now,
        ramp_1=cfg.spam_ramp_hours_1,
        ramp_2=cfg.spam_ramp_hours_2,
        recovery=cfg.spam_recovery_hours,
    )


async def run_spam_check(
    store: Store,
    account: Account | None = None,
    client=None,
    *,
    account_ids: list[int] | None = None,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> SpamCheckResult | str:
    """
    Два режима:
    - (store, account, client, …) → SpamCheckResult: spam_states + dead (autopilot / acc_spam);
    - (store, *, account_ids/bot/…) → str: пакетная проверка accounts.spam_* (restrictions UI).
    """
    if account is not None and client is not None:
        return await _run_spam_check_one(
            store, account, client, force=force, now=now
        )
    return await _run_spam_check_batch(
        store,
        account_ids=account_ids,
        bot=bot,
        admin_chat_id=admin_chat_id,
    )


async def _run_spam_check_one(
    store: Store,
    account: Account,
    client,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> SpamCheckResult:
    """Проверить аккаунт через @SpamBot (по расписанию), обновить состояние и dead-режим."""
    cfg = get_settings()
    res = SpamCheckResult(account=account)
    if account.is_dead and not force:
        res.skipped = "dead"
        return res
    # аутрич: проверяем (для статистики), но dead по повторам не бывает
    state = await store.get_spam_state(account.id)
    if not force and not check_due(state, now=now, every_hours=cfg.spam_check_hours):
        res.skipped = "рано"
        return res
    try:
        status = await check_spambot(client)
    except Exception as e:
        res.error = f"{short_error(e)}"
        # не долбим @SpamBot каждый проход: следующая попытка через spam_check_hours
        state.last_check_at = (now or utcnow()).isoformat(timespec="seconds")
        await store.save_spam_state(state)
        return res
    outcome = evaluate_check(
        state,
        status,
        now=now,
        dead_strikes=cfg.spam_dead_strikes,
        decay_days=cfg.spam_strike_decay_days,
    )
    if not status.known:
        res.error = "ответ @SpamBot не распознан"
        res.outcome = outcome
        return res
    await store.save_spam_state(outcome.state)
    # зеркало в accounts.spam_* для UI ограничений
    stamp = (now or utcnow()).isoformat(timespec="seconds")
    col_status, until, detail = _status_from_spambot(status)
    await store.update_account(
        account.id,
        spam_status=col_status,
        spam_checked_at=stamp,
        spam_until=until,
        spam_detail=detail,
    )
    if col_status == "clean":
        await store.resolve_restrictions_kind(account.id, "spamblock")
    if account.is_outreach:
        # аутрич живёт в спамблоках — dead не нужен (sender и так выключен), сводки не шумим
        outcome.make_dead = False
        res.outcome = outcome
        res.events = []
        return res
    if outcome.make_dead and not account.is_dead:
        await store.update_account(account.id, dead=1)
    res.outcome = outcome
    res.events = list(outcome.events)
    return res


async def _run_spam_check_batch(
    store: Store,
    *,
    account_ids: list[int] | None = None,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
) -> str:
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    if account_ids is not None:
        wanted = set(account_ids)
        accounts = [a for a in accounts if a.id in wanted]
    if not accounts:
        return "SpamBot: нет аккаунтов с Telethon"

    async def _one(acc: Account) -> tuple[Account, str]:
        status = await check_account_spam(store, acc)
        await asyncio.sleep(1.5)  # SpamBot сам режет частые обращения
        return acc, status

    results = await map_batches(accounts, _one, batch_size=3, batch_pause=3.0)
    counts = {"clean": 0, "limited": 0, "unknown": 0}
    limited: list[str] = []
    for res in results:
        if isinstance(res, BaseException):
            counts["unknown"] += 1
            continue
        acc, status = res
        counts[status if status in counts else "unknown"] += 1
        if status == "limited":
            fresh = await store.get_account(acc.id)
            until = f" до {fresh.spam_until}" if fresh and fresh.spam_until else ""
            limited.append(f"{acc.label}{until}")
    summary = (
        f"SpamBot: чистых {counts['clean']}, с ограничением {counts['limited']}, "
        f"не определено {counts['unknown']}"
    )
    if limited:
        summary += "\nОграничены: " + ", ".join(limited)
    if bot and admin_chat_id:
        await safe_send(bot, admin_chat_id, summary)
    return summary


async def run_spam_due(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    limit: int = 6,
) -> str:
    """Постоянная проверка @SpamBot: тик раз в ~30 мин берёт тех, кому пора.

    Пора, если аккаунт ни разу не проверялся, проверка старше spam_recheck_hours,
    или есть флаг «подозрение» (ошибка «banned from sending» без бана в чате).
    О смене статуса (появился лимит / лимит снят) сообщаем сразу.
    """
    from datetime import timedelta

    from app.utils.timefmt import parse_utc

    s = get_settings()
    now = datetime.now(timezone.utc)
    due: list[tuple[int, Account]] = []
    for acc in await store.list_accounts():
        if not acc.telethon_session:
            continue
        flag = parse_utc(await store.get_setting(f"spam_recheck:{acc.id}", ""))
        checked = parse_utc(acc.spam_checked_at)
        flagged = flag is not None and (checked is None or flag > checked)
        stale = checked is None or (now - checked) >= timedelta(hours=s.spam_recheck_hours)
        if flagged or stale:
            due.append((0 if flagged else 1 if checked is None else 2, acc))
    due.sort(key=lambda x: (x[0], x[1].spam_checked_at or ""))
    changes: list[str] = []
    checked_n = 0
    for _prio, acc in due[:limit]:
        before = acc.spam_status
        status = await check_account_spam(store, acc)
        checked_n += 1
        if status == "limited" and before != "limited":
            fresh = await store.get_account(acc.id)
            until = f" до {fresh.spam_until}" if fresh and fresh.spam_until else ""
            changes.append(f"⛔ {acc.label}: @SpamBot — появилось ограничение{until}")
        elif before == "limited" and status == "clean":
            changes.append(f"✅ {acc.label}: ограничение @SpamBot снято")
        await asyncio.sleep(1.5)
    if changes and bot and admin_chat_id:
        await safe_send(bot, admin_chat_id, "\n".join(changes))
    return (
        f"SpamBot: проверено {checked_n}, изменений {len(changes)}, "
        f"в очереди ещё {max(0, len(due) - limit)}"
    )


async def sync_sender_load(
    store: Store,
    account_id: int,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
) -> str:
    """
    Привести Autoposter в соответствие с нагрузкой аккаунта:
    нагрузка 0 / dead / sender выключен → стоп; иначе — перенастройка sender с растянутыми интервалами.
    """
    from app.jobs.setup import run_sender_refresh

    account = await store.get_account(account_id)
    if not account:
        return "аккаунт не найден"
    load = await account_load(store, account)
    sid = (account.sender_account_id or "").strip()
    if account.sender_forbidden or not account.sender_on or load <= 0:
        why = (
            "аутрич (sender не используется)" if account.is_outreach
            else "dead-режим" if account.is_dead
            else "sender отключён вручную" if not account.sender_on
            else "спамблок"
        )
        if sid:
            cfg = get_settings()
            try:
                await SenderAPI(cfg.sender_api_url, cfg.sender_api_key).spam_stop(sid)
            except SenderAPIError as e:
                return f"{why}: Autoposter не остановил — {e}"
        await store.set_applied_load(account.id, 0 if load <= 0 else load)
        return f"{why}: sender остановлен"
    if not account.has_sender:
        await store.set_applied_load(account.id, load)
        return f"нагрузка {load}% (sender не настроен)"
    n = await run_sender_refresh(store, account.id, bot, admin_chat_id, job_kind="sender_load")
    return f"нагрузка sender {load}%, чатов в рассылке: {n}"


def load_label(load: int) -> str:
    return {100: "100%", 50: "50% (спамблок/восстановление)", 25: "25% (спамблок)", 0: "0% (стоп)"}.get(
        load, f"{load}%"
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def state_summary(state: SpamState) -> str:
    return f"{state.status}, повторов: {state.strikes}"
