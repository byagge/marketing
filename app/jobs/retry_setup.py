from __future__ import annotations

import logging
from collections import defaultdict

from aiogram import Bot

from app.config import get_settings
from app.jobs.health import run_health_all
from app.jobs.setup import run_setup_chats_only
from app.store import Store

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

    ok_n = skip_n = abandoned_n = 0
    for account_id, chat_pks in by_account.items():
        try:
            buckets = await run_setup_chats_only(
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

    ok_n = skip_n = abandoned_n = 0
    for account_id, chat_pks in by_account.items():
        try:
            buckets = await run_setup_chats_only(
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


def setup_retry_cron_args() -> dict:
    settings = get_settings()
    return {
        "hour": settings.setup_retry_hour,
        "minute": 0,
        "timezone": settings.timezone,
    }
