"""Сторож простоя: закрыть зависшие задачи и написать админу.

Рестарт процесса делает внешний scripts/stall_watchdog.py (systemd timer):
если завис сам event loop, этот код уже не выполнится.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.config import get_settings
from app.notify import safe_send
from app.store import Store

log = logging.getLogger("marketing.stall")

HARD_MINUTES = 20
FACTS_MAX_AGE_H = 2
SEND_GAP_MIN = 30
DAILY_MAX_AGE_H = 26


async def run_stall_guard(store: Store, bot: Bot | None = None, admin_id: int | None = None) -> str:
    closed = await store.fail_stale_jobs(HARD_MINUTES)
    reasons = await stall_reasons(store)
    summary = f"stall_guard: closed={closed}" + (f" | {'; '.join(reasons)}" if reasons else " | ok")
    if reasons and bot and admin_id:
        last = await store.get_setting("stall_alert_at", "")
        now = datetime.now(timezone.utc)
        prev = None
        if last:
            try:
                prev = datetime.fromisoformat(last)
                if prev.tzinfo is None:
                    prev = prev.replace(tzinfo=timezone.utc)
            except ValueError:
                prev = None
        if prev is None or (now - prev).total_seconds() >= 30 * 60:
            settings = get_settings()
            mention = (settings.notify_mention or "").strip()
            head = f"{mention} " if mention else ""
            text = head + "Маркетинг простаивает:\n" + "\n".join(f"· {r}" for r in reasons)
            text += "\nСторож перезапустит сервис, если простой не уйдёт."
            await safe_send(bot, admin_id, text[:4000])
            await store.set_setting("stall_alert_at", now.isoformat(timespec="seconds"))
    log.info("%s", summary)
    return summary


async def stall_reasons(store: Store) -> list[str]:
    """Причины простоя по БД. Пустой список — всё живое."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    local = datetime.now(settings.tz)
    reasons: list[str] = []

    hard = await store.raw_rows(
        "SELECT kind, COUNT(*) n, MIN(started_at) oldest FROM jobs "
        "WHERE status='running' AND started_at < ? "
        "AND kind NOT IN ('setup_daily_nonpremium','setup','setup_retry','health') "
        "GROUP BY kind",
        ((now - timedelta(minutes=HARD_MINUTES)).isoformat(timespec="seconds"),),
    )
    for row in hard:
        reasons.append(f"{row['kind']} висит running ×{row['n']} с {row['oldest']}")

    if settings.start_hour <= local.hour <= 23:
        gap = await store.raw_rows(
            "SELECT MAX(sent_at) AS last_sent FROM send_events"
        )
        last_sent = (gap[0]["last_sent"] if gap else "") or ""
        silent = True
        if last_sent:
            try:
                sent = datetime.fromisoformat(last_sent)
                if sent.tzinfo is None:
                    sent = sent.replace(tzinfo=timezone.utc)
                silent = (now - sent).total_seconds() > SEND_GAP_MIN * 60
            except ValueError:
                silent = True
        if silent:
            reasons.append(f"нет отправок больше {SEND_GAP_MIN} мин в рабочее окно")

    facts = await store.raw_rows("SELECT MAX(checked_at) AS checked FROM chat_facts")
    checked = (facts[0]["checked"] if facts else "") or ""
    facts_old = True
    if checked:
        try:
            dt = datetime.fromisoformat(checked)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            facts_old = (now - dt).total_seconds() > FACTS_MAX_AGE_H * 3600
        except ValueError:
            facts_old = True
    if facts_old:
        reasons.append(f"факты старше {FACTS_MAX_AGE_H} ч")

    daily = await store.raw_rows(
        "SELECT MAX(finished_at) AS done FROM jobs "
        "WHERE kind='setup_daily_nonpremium' AND status='done'"
    )
    done = (daily[0]["done"] if daily else "") or ""
    daily_old = True
    if done:
        try:
            dt = datetime.fromisoformat(done)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            daily_old = (now - dt).total_seconds() > DAILY_MAX_AGE_H * 3600
        except ValueError:
            daily_old = True
    if daily_old and local.hour >= settings.nonpremium_hour:
        reasons.append(f"суточная настройка не проходила {DAILY_MAX_AGE_H} ч")
    return reasons
