from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta
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
from app.jobs.health import weekly_cron_args
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

    scheduler = AsyncIOScheduler(timezone=settings.timezone)

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
    # Interval comes from bot UI (DB). Tick every 15 min and run when due.
    scheduler.add_job(
        online_tick,
        "interval",
        id="online_ping_tick",
        replace_existing=True,
        minutes=15,
    )
    run_at = datetime.now(scheduler.timezone) + timedelta(seconds=45)
    scheduler.add_job(
        online_tick,
        "date",
        id="online_ping_startup",
        replace_existing=True,
        run_date=run_at,
    )
    scheduler.start()

    online_cfg = await store.online_ping_settings()
    log.info(
        "Marketing bot starting (weekly=%s %s:00, setup_retry=daily %s:00, "
        "nonpremium_reschedule=daily %s:00, online_ping=%s every %sh±%ss, "
        "retry_days=%s, max_attempts=%s)",
        settings.weekly_health_dow,
        settings.weekly_health_hour,
        settings.setup_retry_hour,
        settings.nonpremium_hour,
        "on" if online_cfg.enabled else "off",
        online_cfg.hours,
        online_cfg.jitter_sec,
        settings.setup_retry_days,
        settings.setup_max_attempts,
    )
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
