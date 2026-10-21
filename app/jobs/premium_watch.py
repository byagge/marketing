"""Слежение за окончанием Premium: предупреждение за ≤24 ч + факт «закончился»."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot

from app.config import get_settings
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.notify import safe_send
from app.store import Store
from app.tg.client import telethon_client
from app.tg.premium import expire_lapsed_premium, needs_warn, refresh_account_premium
from app.ui.emoji import pe
from app.utils.errfmt import short_error
from app.utils.timefmt import fmt_local, parse_utc, to_iso

log = logging.getLogger("marketing.jobs.premium_watch")


def _mark(label: str) -> str:
    return f"{pe('star')} <b>{escape(label)}</b>"


async def run_premium_watch(store: Store, bot: Bot | None = None, admin_id: int | None = None) -> str:
    """Пройти аккаунты с Telethon: обновить Premium и предупредить админа."""
    settings = get_settings()
    admin_id = admin_id or next(iter(settings.admins), None)
    now = datetime.now(timezone.utc)
    # Флаг не ждёт Telethon: промо с прошедшим premium_until снимается сразу.
    lapsed = await expire_lapsed_premium(store, now=now)
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    if not accounts:
        return "premium_watch: нет аккаунтов" + (
            f", снято: {', '.join(lapsed)}" if lapsed else ""
        )

    batch_size, batch_pause = setup_parallel_defaults()
    warns: list[str] = []
    ended: list[str] = []
    errors = 0
    lapsed_labels = set(lapsed)
    for label in lapsed:
        ended.append(f"{_mark(label)} — Premium <b>закончился</b> (срок в базе уже прошёл)")

    async def _one(acc):
        nonlocal errors
        was_prem = bool(acc.is_premium)
        try:
            async with telethon_client(acc.telethon_session) as client:
                info = await refresh_account_premium(store, client, acc, now=now)
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.info("premium_watch %s: %s", acc.label, short_error(e))
            return None
        fresh = await store.get_account(acc.id) or acc
        if was_prem and not info.is_premium and acc.label not in lapsed_labels:
            was_until = parse_utc(acc.premium_until)
            was_s = fmt_local(was_until, "%d.%m.%Y") if was_until else "—"
            ended.append(
                f"{_mark(acc.label)} — Premium <b>закончился</b> (был до {escape(was_s)})"
            )
            return "ended"
        if info.is_premium and fresh.premium_until and needs_warn(fresh.premium_until, now=now):
            warned = parse_utc(fresh.premium_warned_at)
            if warned is None or (now - warned).total_seconds() >= 20 * 3600:
                until_dt = parse_utc(fresh.premium_until)
                left_h = (
                    max(0, int((until_dt - now).total_seconds() // 3600))
                    if until_dt
                    else 0
                )
                until_s = fmt_local(until_dt, "%d.%m.%Y %H:%M") if until_dt else "—"
                src = fresh.premium_until_source or "?"
                warns.append(
                    f"{_mark(acc.label)} — Premium <b>заканчивается</b> "
                    f"{escape(until_s)} (≈{left_h} ч, источник: {escape(src)})"
                )
                await store.update_account(
                    acc.id, premium_warned_at=to_iso(now)
                )
                return "warn"
        return "ok"

    await map_batches(accounts, _one, batch_size=batch_size, batch_pause=batch_pause)

    lines: list[str] = []
    if warns:
        lines.append(f"{pe('warning')} <b>Premium заканчивается</b> (≤24 ч):")
        lines.extend(f"· {w}" for w in warns)
    if ended:
        if lines:
            lines.append("")
        lines.append(f"{pe('down')} <b>Premium закончился</b>:")
        lines.extend(f"· {w}" for w in ended)

    if lines and bot and admin_id:
        mention = (settings.notify_mention or "").strip()
        head = f"{mention} " if mention else ""
        await safe_send(
            bot,
            admin_id,
            head + "\n".join(lines),
            parse_mode="HTML",
        )

    summary = (
        f"premium_watch: accounts={len(accounts)} warn={len(warns)} "
        f"ended={len(ended)} err={errors}"
    )
    log.info("%s", summary)
    return summary
