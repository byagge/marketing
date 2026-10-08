"""Спамблок аккаунта: проверка @SpamBot, ступени нагрузки sender, dead-режим."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram import Bot

from app.config import get_settings
from app.models import Account, SpamState
from app.sender_api import SenderAPI, SenderAPIError
from app.store import Store
from app.tg.spambot import check_spambot
from app.utils.spamstate import (
    CheckOutcome,
    check_due,
    compute_load,
    evaluate_check,
)


@dataclass
class SpamCheckResult:
    account: Account
    outcome: CheckOutcome | None = None
    skipped: str = ""
    error: str = ""
    events: list[str] = field(default_factory=list)


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
        res.error = f"{type(e).__name__}: {e}"
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
