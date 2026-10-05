from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot import setup_routers
from app.bot.middlewares import AdminOnlyMiddleware
from app.config import ensure_dirs, get_settings
from app.context import ctx
from app.jobs.balance import run_smart_rebalance
from app.jobs.health import weekly_cron_args
from app.jobs.marketer import (
    marketer_cron_args,
    marketer_morning_cron_args,
    run_marketer_morning,
    run_marketer_tick,
)
from app.jobs.online import run_online_ping_tick
from app.jobs.retry_setup import (
    nonpremium_reschedule_cron_args,
    run_nonpremium_daily_reschedule,
    run_setup_retries,
    run_weekly_recheck,
    setup_retry_cron_args,
)
from app.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("marketing")


async def main() -> None:
    ensure_dirs()
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("Задайте BOT_TOKEN в .env (см. .env.example)")
    if not settings.admins:
        log.warning("ADMIN_IDS пуст — бот отвечает всем. Заполните .env")

    store = Store()
    await store.init()
    ctx.store = store

    bot = Bot(token=settings.bot_token)
    ctx.bot = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(AdminOnlyMiddleware())
    setup_routers(dp)

    # misfire_grace_time: по умолчанию 1 с — если цикл событий занят, cron-задача
    # (утренние отчёты, суточный schedule) молча пропускалась.
    scheduler = AsyncIOScheduler(
        timezone=settings.timezone,
        job_defaults={"misfire_grace_time": 3600, "coalesce": True, "max_instances": 1},
    )

    async def weekly():
        admin_id = next(iter(settings.admins), None)
        if admin_id:
            await run_weekly_recheck(store, bot, admin_id)

    async def daily_retry():
        # Runs daily; only chats whose last attempt was ≥ retry_days ago are processed.
        admin_id = next(iter(settings.admins), None)
        await run_setup_retries(store, bot, admin_id)

    async def daily_nonpremium():
        # Non-Premium: no schedule_repeat_period — rebuild the day's grid.
        # Premium accounts are skipped (Telegram daily repeat stays intact).
        admin_id = next(iter(settings.admins), None)
        await run_nonpremium_daily_reschedule(store, bot, admin_id)

    async def facts_tick():
        # раз в час: факты отправки + (каждые N часов) скан банов/мутов
        from datetime import datetime

        from app.jobs.facts import spawn_facts

        hour = datetime.now(settings.tz).hour
        scan = hour % max(1, settings.restrictions_scan_every_hours) == 0
        if not scan:
            # мут закончился → проверяем сразу, а не ждём плановый скан
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            scan = bool(await store.list_restrictions_expired(now_iso))
        admin_id = next(iter(settings.admins), None)
        spawn_facts(
            store,
            hours=settings.facts_hours,
            scan_restrictions=scan,
            bot=bot,
            admin_chat_id=admin_id,
            quiet=True,
        )

    async def spam_tick():
        # постоянная проверка @SpamBot: по 6 аккаунтов за тик, у кого пора / подозрение
        from app.jobs.spam import run_spam_due

        admin_id = next(iter(settings.admins), None)
        summary = await run_spam_due(store, bot, admin_id)
        log.info("%s", summary)

    async def bans_weekly():
        # бан-база: раз в неделю только читаем статус (не вступаем/не пишем)
        from app.jobs.bans import recheck_bans

        admin_id = next(iter(settings.admins), None)
        log.info("%s", await recheck_bans(store, bot, admin_id))

    async def advisor_tick():
        from app.jobs.advisor import run_advisor

        admin_id = next(iter(settings.admins), None)
        await run_advisor(store, bot, admin_id)

    async def marketer_tick():
        if not settings.marketer_enabled:
            return
        enabled = (await store.get_setting("marketer_enabled", "1")) == "1"
        if not enabled:
            return
        summary = await run_marketer_tick(store, bot)
        log.info("%s", summary)

    async def marketer_morning():
        if not settings.marketer_enabled:
            return
        enabled = (await store.get_setting("marketer_enabled", "1")) == "1"
        if not enabled:
            return
        summary = await run_marketer_morning(store, bot)
        log.info("%s", summary)

    async def auto_rebalance():
        # Факты → кто реально пишет → равномерные слоты. Молча, если менять нечего.
        admin_id = next(iter(settings.admins), None)
        try:
            await run_smart_rebalance(store, bot, admin_id, collect=True, notify="changes")
        except Exception:
            log.exception("auto rebalance failed")

    async def online_tick():
        summary = await run_online_ping_tick(store)
        log.info("%s", summary)

    scheduler.add_job(
        weekly, "cron", id="weekly_health", replace_existing=True, **weekly_cron_args()
    )
    scheduler.add_job(
        daily_retry,
        "cron",
        id="setup_retry_daily",
        replace_existing=True,
        **setup_retry_cron_args(),
    )
    scheduler.add_job(
        daily_nonpremium,
        "cron",
        id="nonpremium_reschedule_daily",
        replace_existing=True,
        **nonpremium_reschedule_cron_args(),
    )
    scheduler.add_job(
        facts_tick,
        "cron",
        id="facts_hourly",
        replace_existing=True,
        minute=settings.facts_minute,
        timezone=settings.timezone,
        misfire_grace_time=900,
        coalesce=True,
    )
    scheduler.add_job(
        spam_tick,
        "interval",
        id="spam_due",
        replace_existing=True,
        minutes=30,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        bans_weekly,
        "cron",
        id="bans_weekly",
        replace_existing=True,
        day_of_week="mon",
        hour=11,
        minute=40,
        timezone=settings.timezone,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.add_job(
        advisor_tick,
        "cron",
        id="advisor_daily",
        replace_existing=True,
        hour=settings.advisor_hour,
        minute=17,
        timezone=settings.timezone,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Interval comes from bot UI (DB). Tick every 15 min and run when due.
    scheduler.add_job(
        online_tick,
        "interval",
        id="online_ping_tick",
        replace_existing=True,
        minutes=15,
    )
    if settings.auto_rebalance:
        scheduler.add_job(
            auto_rebalance,
            "interval",
            id="smart_rebalance",
            replace_existing=True,
            hours=max(0.5, float(settings.rebalance_every_hours)),
            next_run_time=datetime.now(scheduler.timezone) + timedelta(minutes=20),
        )
    run_at = datetime.now(scheduler.timezone) + timedelta(seconds=45)
    scheduler.add_job(
        online_tick,
        "date",
        id="online_ping_startup",
        replace_existing=True,
        run_date=run_at,
    )
    # AI-маркетолог: тихий сбор 24/7 + утренний брифинг.
    mk_args = marketer_cron_args()
    scheduler.add_job(
        marketer_tick,
        "interval",
        id="marketer_tick",
        replace_existing=True,
        minutes=mk_args["minutes"],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    mk_startup = datetime.now(scheduler.timezone) + timedelta(minutes=2)
    scheduler.add_job(
        marketer_tick,
        "date",
        id="marketer_startup",
        replace_existing=True,
        run_date=mk_startup,
    )
    morning_args = marketer_morning_cron_args()
    scheduler.add_job(
        marketer_morning,
        "cron",
        id="marketer_morning",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        **morning_args,
    )
    scheduler.start()

    online_cfg = await store.online_ping_settings()
    log.info(
        "Marketing bot starting (weekly=%s %s:00, setup_retry=daily %s:00, "
        "nonpremium_reschedule=daily %s:00, online_ping=%s every %sh±%ss, "
        "marketer=every %smin + morning %02d:%02d, retry_days=%s, max_attempts=%s, "
        "auto_rebalance=%s every %sh)",
        settings.weekly_health_dow,
        settings.weekly_health_hour,
        settings.setup_retry_hour,
        settings.nonpremium_hour,
        "on" if online_cfg.enabled else "off",
        online_cfg.hours,
        online_cfg.jitter_sec,
        settings.marketer_interval_min,
        settings.marketer_morning_hour,
        settings.marketer_morning_minute,
        settings.setup_retry_days,
        settings.setup_max_attempts,
        "on" if settings.auto_rebalance else "off",
        settings.rebalance_every_hours,
    )
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
