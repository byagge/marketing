from __future__ import annotations

import logging
from collections import defaultdict

from aiogram import Bot

from app.config import get_settings
from app.jobs.health import run_health_all
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.jobs.setup import run_setup_chats_only
from app.store import Store
from app.tg.client import telethon_client

log = logging.getLogger("marketing.retry")


async def run_setup_retries(store: Store, bot: Bot | None, admin_chat_id: int | None) -> str:
    """Retry schedule for chats that failed earlier and are due (every ~3 days)."""
    settings = get_settings()
    due = await store.list_due_setup_retries(
        retry_days=settings.setup_retry_days,
        max_attempts=settings.setup_max_attempts,
    )
    if not due:
        return "Повтор настройки: нет чатов к проверке"

    by_account: dict[int, list[int]] = defaultdict(list)
    titles: dict[int, str] = {}
    for state in due:
        by_account[state.account_id].append(state.chat_pk)
        titles[state.chat_pk] = state.chat_title or str(state.chat_pk)

    if bot and admin_chat_id:
        try:
            await bot.send_message(
                admin_chat_id,
                f"Повтор настройки schedule | {len(due)} чатов / "
                f"{len(by_account)} аккаунтов "
                f"(каждые {settings.setup_retry_days} дн., "
                f"макс {settings.setup_max_attempts} попыток)",
            )
        except Exception:
            pass

    batch_size, batch_pause = setup_parallel_defaults()
    items = list(by_account.items())
    ok_n = skip_n = abandoned_n = 0

    async def _one(item: tuple[int, list[int]]):
        account_id, chat_pks = item
        try:
            return await run_setup_chats_only(
                store,
                account_id,
                chat_pks,
                bot,
                admin_chat_id,
                job_kind="setup_retry",
                skip_abandoned=True,
            )
        except Exception as e:
            log.exception("setup retry failed for account %s: %s", account_id, e)
            return {"ok": [], "skipped": [], "abandoned": [], "config_error": []}

    results = await map_batches(
        items, _one, batch_size=batch_size, batch_pause=batch_pause
    )
    for buckets in results:
        if isinstance(buckets, BaseException):
            continue
        ok_n += len(buckets.get("ok", []))
        skip_n += len(buckets.get("skipped", [])) + len(buckets.get("config_error", []))
        abandoned_n += len(buckets.get("abandoned", []))

    summary = (
        f"Повтор настройки завершён: ок={ok_n}, пропуск={skip_n}, "
        f"стоп до недели={abandoned_n}"
    )
    if bot and admin_chat_id:
        try:
            await bot.send_message(admin_chat_id, summary)
        except Exception:
            pass
    return summary


async def run_weekly_recheck(store: Store, bot: Bot, admin_chat_id: int) -> None:
    """
    Start of week:
    1. Reset pending/abandoned counters so chats can be tried again
    2. Re-check every non-ok schedule chat separately
    3. Run the usual health report

    Premium accounts keep Telegram daily repeat — this job does NOT wipe
    working schedules. Non-Premium renewal is handled by the daily cron.
    """
    settings = get_settings()
    reset_n = await store.reset_setup_states_for_weekly(include_abandoned=True)
    pending = await store.list_setup_states(statuses=["pending"])
    # After reset, last_attempt_at is kept but fail_count=0 — force all pending into a try.
    # Also include enabled schedule chats that never got a state yet? Weekly focuses on
    # previously failed ones; brand-new chats are handled by manual setup.
    by_account: dict[int, list[int]] = defaultdict(list)
    for state in pending:
        by_account[state.account_id].append(state.chat_pk)

    await bot.send_message(
        admin_chat_id,
        f"Недельный пересмотр schedule | сброшено состояний: {reset_n} | "
        f"к проверке: {len(pending)} чатов",
    )

    batch_size, batch_pause = setup_parallel_defaults()
    items = list(by_account.items())
    ok_n = skip_n = abandoned_n = 0

    async def _one(item: tuple[int, list[int]]):
        account_id, chat_pks = item
        try:
            return await run_setup_chats_only(
                store,
                account_id,
                chat_pks,
                bot,
                admin_chat_id,
                job_kind="setup_weekly",
                skip_abandoned=False,
            )
        except Exception as e:
            log.exception("weekly recheck failed for account %s: %s", account_id, e)
            return {"ok": [], "skipped": [], "abandoned": [], "config_error": []}

    results = await map_batches(
        items, _one, batch_size=batch_size, batch_pause=batch_pause
    )
    for buckets in results:
        if isinstance(buckets, BaseException):
            continue
        ok_n += len(buckets.get("ok", []))
        skip_n += len(buckets.get("skipped", [])) + len(buckets.get("config_error", []))
        abandoned_n += len(buckets.get("abandoned", []))

    await bot.send_message(
        admin_chat_id,
        f"Недельный пересмотр: ок={ok_n}, пропуск={skip_n}, "
        f"снова стоп={abandoned_n} (макс {settings.setup_max_attempts} попыток)",
    )
    await run_health_all(store, bot, admin_chat_id)


async def run_nonpremium_daily_reschedule(
    store: Store, bot: Bot | None, admin_chat_id: int | None
) -> str:
    """
    Non-Premium accounts cannot use schedule_repeat_period (Telegram 403).
    Each day, re-clear and re-schedule chats that previously succeeded (status=ok).

    Premium accounts are skipped — their Telegram 24h repeat stays untouched.
    Pending/abandoned chats stay with the 3-day retry and weekly jobs.
    """
    settings = get_settings()
    schedule_chats = await store.list_chats(kind="schedule", enabled_only=True)
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    if not schedule_chats:
        return "Суточный schedule (non-Premium): нет schedule-чатов"
    if not accounts:
        return "Суточный schedule (non-Premium): нет аккаунтов с Telethon"

    chat_pks = [c.id for c in schedule_chats]
    if bot and admin_chat_id:
        try:
            await bot.send_message(
                admin_chat_id,
                f"Суточный schedule (non-Premium) | проверка {len(accounts)} аккаунтов, "
                f"{len(chat_pks)} чатов | час={settings.nonpremium_hour}:00",
            )
        except Exception:
            pass

    renewed = skipped_premium = 0
    ok_n = skip_n = abandoned_n = 0
    enabled_pks = set(chat_pks)
    batch_size, batch_pause = setup_parallel_defaults()

    async def _one(acc):
        nonlocal skipped_premium
        try:
            async with telethon_client(acc.telethon_session) as client:
                me = await client.get_me()
                is_premium = bool(getattr(me, "premium", False))
            await store.update_account(acc.id, is_premium=1 if is_premium else 0)
            if is_premium:
                return ("premium", None)
            ok_states = await store.list_setup_states(acc.id, statuses=["ok"])
            renew_pks = [s.chat_pk for s in ok_states if s.chat_pk in enabled_pks]
            if not renew_pks:
                return ("skip", None)
            buckets = await run_setup_chats_only(
                store,
                acc.id,
                renew_pks,
                bot,
                admin_chat_id,
                job_kind="setup_daily_nonpremium",
                skip_abandoned=True,
            )
            return ("ok", buckets)
        except Exception as e:
            log.exception("nonpremium daily failed for account %s: %s", acc.id, e)
            return ("err", None)

    results = await map_batches(
        accounts, _one, batch_size=batch_size, batch_pause=batch_pause
    )
    for r in results:
        if isinstance(r, BaseException):
            continue
        kind, buckets = r
        if kind == "premium":
            skipped_premium += 1
        elif kind == "ok" and buckets:
            renewed += 1
            ok_n += len(buckets.get("ok", []))
            skip_n += len(buckets.get("skipped", [])) + len(
                buckets.get("config_error", [])
            )
            abandoned_n += len(buckets.get("abandoned", []))

    summary = (
        f"Суточный schedule (non-Premium): обновлено аккаунтов={renewed}, "
        f"пропуск Premium={skipped_premium}, "
        f"чаты ок={ok_n}, пропуск={skip_n}, abandoned={abandoned_n}"
    )
    if bot and admin_chat_id:
        try:
            await bot.send_message(admin_chat_id, summary)
        except Exception:
            pass
    return summary


def setup_retry_cron_args() -> dict:
    settings = get_settings()
    return {
        "hour": settings.setup_retry_hour,
        "minute": 0,
        "timezone": settings.timezone,
    }


def nonpremium_reschedule_cron_args() -> dict:
    settings = get_settings()
    return {
        "hour": settings.nonpremium_hour,
        "minute": 0,
        "timezone": settings.timezone,
    }
