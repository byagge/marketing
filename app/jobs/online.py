from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.jobs import runtime
from app.store import Store
from app.tg.client import telethon_client
from app.tg.online import bump_online

log = logging.getLogger("marketing.online")


def _parse_iso(raw: str) -> datetime | None:
    if not (raw or "").strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def run_online_ping(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    notify: bool = False,
    force: bool = False,
) -> str:
    """
    Briefly set Online on Telethon accounts with online_ping enabled.
    Skips accounts mid-setup. Respects global Online settings from DB.
    """
    cfg = await store.online_ping_settings()
    if not cfg.enabled and not force:
        return "Online ping: выключен глобально"

    accounts = [
        a
        for a in await store.list_accounts()
        if a.telethon_session and a.online_ping_enabled
    ]

    async def _schedule_next() -> datetime:
        now = datetime.now(timezone.utc)
        jitter = random.randint(-cfg.jitter_sec, cfg.jitter_sec) if cfg.jitter_sec else 0
        delay = max(1800.0, cfg.hours * 3600.0 + jitter)
        next_at = now + timedelta(seconds=delay)
        await store.update_online_ping_settings(
            last_at=now.isoformat(timespec="seconds"),
            next_at=next_at.isoformat(timespec="seconds"),
        )
        return next_at

    if not accounts:
        next_at = await _schedule_next()
        return (
            "Online ping: нет подходящих аккаунтов (Telethon + ping вкл.) | "
            f"след. ~{next_at.astimezone().strftime('%d.%m %H:%M')}"
        )

    ok = fail = skip = 0
    errors: list[str] = []
    for acc in accounts:
        if runtime.is_running("setup", acc.id):
            skip += 1
            continue
        try:
            async with telethon_client(acc.telethon_session) as client:
                await bump_online(client, hold_seconds=cfg.hold_seconds)
            ok += 1
            log.info("online ping ok | %s (#%s)", acc.label, acc.id)
        except Exception as e:
            fail += 1
            err = f"{acc.label}: {type(e).__name__}: {e}"
            errors.append(err)
            log.warning("online ping fail | %s", err)

    next_at = await _schedule_next()
    summary = (
        f"Online ping: ok={ok}, fail={fail}, skip={skip} "
        f"(аккаунтов {len(accounts)}) | след. ~{next_at.astimezone().strftime('%d.%m %H:%M')}"
    )
    if errors:
        summary += "\n" + "\n".join(errors[:8])
    if notify and bot and admin_chat_id and (fail or ok):
        try:
            await bot.send_message(admin_chat_id, summary)
        except Exception:
            pass
    return summary


async def run_online_ping_tick(store: Store) -> str:
    """Periodic tick: run only when due (interval comes from bot settings)."""
    cfg = await store.online_ping_settings()
    if not cfg.enabled:
        return "Online ping: выключен"
    now = datetime.now(timezone.utc)
    next_at = _parse_iso(cfg.next_at)
    if next_at is not None and now < next_at:
        return f"Online ping: ждём до {next_at.astimezone().strftime('%d.%m %H:%M')}"
    return await run_online_ping(store)
