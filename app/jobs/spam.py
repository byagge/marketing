"""Проверка @SpamBot у аккаунтов и сохранение статуса."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot

from app.jobs.parallel import map_batches
from app.models import Account
from app.notify import safe_send
from app.store import Store
from app.tg.client import telethon_client
from app.tg.spambot import check_spambot

log = logging.getLogger("marketing.spam")

STATUS_LABEL = {
    "clean": "чист",
    "limited": "СПАМ-ОГРАНИЧЕН",
    "unknown": "не определён",
    "": "не проверялся",
}


async def check_account_spam(store: Store, account: Account) -> str:
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
            spam_detail=f"{type(e).__name__}: {e}"[:300],
        )
        return "unknown"
    await store.update_account(
        account.id,
        spam_status=res.status,
        spam_checked_at=now,
        spam_until=res.until,
        spam_detail=res.detail,
    )
    return res.status


async def run_spam_check(
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

    # SpamBot — один бот на всех: небольшими пачками
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

    from app.config import get_settings
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
    return f"SpamBot: проверено {checked_n}, изменений {len(changes)}, в очереди ещё {max(0, len(due) - limit)}"
