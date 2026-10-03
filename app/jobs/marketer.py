"""Cron / manual entrypoints for the AI marketer."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from app.config import get_settings
from app.jobs import runtime
from app.marketer.brain import run_marketer_once
from app.store import Store

log = logging.getLogger("marketing.jobs.marketer")

# Глобальный замок: тихий сбор и утро не пересекаются.
_marketer_lock = asyncio.Lock()


async def run_marketer_tick(store: Store, bot: Bot | None = None) -> str:
    """Тихий 24/7 сбор. Без параллельных запусков."""
    if _marketer_lock.locked() or runtime.is_running("marketer", 0):
        return "marketer: skip (already running)"

    settings = get_settings()
    admin_id = next(iter(settings.admins), None)

    async with _marketer_lock:
        # Пометить для request_cancel из UI/стопа
        box: dict = {}

        async def _job():
            box["result"] = await run_marketer_once(
                store, bot, admin_id, notify=False, morning=False
            )

        try:
            task = runtime.spawn("marketer", 0, _job())
        except RuntimeError:
            return "marketer: skip (already running)"
        await task
        result = box.get("result")
        if result is None:
            return "marketer: failed (no result)"
        return (
            f"operator: pairs={result.pairs} working={result.working} "
            f"actions={len(result.actions)} (ok={sum(1 for a in result.actions if a.ok)}) "
            f"new_escalations={len(result.new_escalations)}"
        )


async def run_marketer_morning(store: Store, bot: Bot | None = None) -> str:
    """Ежедневный брифинг 07:40 — ждёт окончания тихого сбора при необходимости."""
    settings = get_settings()
    admin_id = next(iter(settings.admins), None)

    # До 15 мин ждём тихий тик
    for _ in range(90):
        if not _marketer_lock.locked() and not runtime.is_running("marketer", 0):
            break
        await asyncio.sleep(10)
    else:
        if runtime.is_running("marketer", 0):
            runtime.request_cancel("marketer", 0)
            await asyncio.sleep(3)

    async with _marketer_lock:
        box: dict = {}

        async def _job():
            box["result"] = await run_marketer_once(
                store, bot, admin_id, notify=True, morning=True
            )

        try:
            task = runtime.spawn("marketer", 0, _job())
            await task
        except RuntimeError:
            # Замок наш, но runtime занят — выполним напрямую
            box["result"] = await run_marketer_once(
                store, bot, admin_id, notify=True, morning=True
            )
        result = box.get("result")
        if result is None:
            return "marketer_morning: failed"
        return (
            f"operator_morning: pairs={result.pairs} working={result.working} "
            f"actions={len(result.actions)}"
        )


def marketer_cron_args() -> dict:
    settings = get_settings()
    return {
        "minutes": max(10, int(settings.marketer_interval_min)),
        "timezone": settings.timezone,
    }


def marketer_morning_cron_args() -> dict:
    settings = get_settings()
    return {
        "hour": int(settings.marketer_morning_hour),
        "minute": int(settings.marketer_morning_minute),
        "timezone": settings.timezone,
    }
