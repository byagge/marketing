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
