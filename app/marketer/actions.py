"""Автопочинка: то, что маркетолог может сделать сам без человека."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot

from app.config import get_settings
from app.jobs import LogSink
from app.marketer.scan import Finding
from app.models import Account
from app.sender_api import SenderAPI
from app.store import Store
from app.tg.client import telethon_client

log = logging.getLogger("marketing.marketer.actions")


@dataclass
class FixResult:
    finding_kind: str
    account_id: int
    chat_pk: int | None
    ok: bool
    message: str


async def apply_fix(
    store: Store,
    finding: Finding,
    *,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
) -> FixResult:
    account = await store.get_account(finding.account_id) if finding.account_id else None
    if not account:
        return FixResult(
            finding.kind,
            finding.account_id or 0,
            finding.chat_pk,
            False,
            "аккаунт не найден",
        )

    try:
        if finding.kind == "premium_lost":
            await store.update_account(account.id, is_premium=0)
            await store.resolve_incidents_matching(
                kind="premium_lost", account_id=account.id
            )
            return FixResult(
                finding.kind,
                account.id,
                None,
                True,
                f"{account.label}: снял флаг Premium (слотевшая подписка)",
            )

        if finding.kind == "premium_gained":
            await store.update_account(account.id, is_premium=1)
            await store.resolve_incidents_matching(
                kind="premium_gained", account_id=account.id
            )
            return FixResult(
                finding.kind,
                account.id,
                None,
                True,
                f"{account.label}: включил Premium в БД",
            )

        if finding.kind in {"sender_offline", "sender_stopped"}:
            return await _fix_sender(store, account, finding)

        if finding.kind in {"not_assigned", "schedule_empty", "schedule_low"}:
            return await _fix_schedule(store, account, finding, bot, admin_chat_id)

    except Exception as e:
        log.exception("fix failed %s acc=%s", finding.kind, account.id)
        return FixResult(
            finding.kind,
            account.id,
            finding.chat_pk,
            False,
            f"{type(e).__name__}: {e}",
        )

    return FixResult(
        finding.kind,
        account.id,
        finding.chat_pk,
        False,
        "нет автопочинки для этого типа",
    )


async def _fix_sender(store: Store, account: Account, finding: Finding) -> FixResult:
    sender_id = (account.sender_account_id or "").strip()
    if not sender_id:
        return FixResult(
            finding.kind, account.id, None, False, "нет sender_account_id"
        )
    settings = get_settings()
    api = SenderAPI(settings.sender_api_url, settings.sender_api_key)
    if finding.kind == "sender_offline":
        await api.start_account(sender_id)
        await api.spam_start(sender_id)
        msg = f"{account.label}: поднял sender + spam/start"
    else:
        await api.spam_start(sender_id)
        msg = f"{account.label}: перезапустил spam/start"
    await store.resolve_incidents_matching(kind=finding.kind, account_id=account.id)
    return FixResult(finding.kind, account.id, None, True, msg)


async def _fix_schedule(
    store: Store,
    account: Account,
    finding: Finding,
    bot: Bot | None,
    admin_chat_id: int | None,
) -> FixResult:
    from app.jobs.setup import schedule_one_chat

    if not finding.chat_pk:
        return FixResult(
            finding.kind, account.id, None, False, "нет chat_pk"
        )
    chat = await store.get_chat(finding.chat_pk)
    if not chat or not chat.is_schedule:
        return FixResult(
            finding.kind, account.id, finding.chat_pk, False, "чат не schedule"
        )
    if not account.telethon_session:
        return FixResult(
            finding.kind, account.id, finding.chat_pk, False, "нет Telethon"
        )

    posts = {
        "ru": await store.get_post(account.id, "ru"),
        "en": await store.get_post(account.id, "en"),
        "ru_short": await store.get_post(account.id, "ru_short"),
        "en_short": await store.get_post(account.id, "en_short"),
    }
    # Refresh premium flag
    acc = await store.get_account(account.id) or account
    job = await store.create_job("marketer_fix", account.id)
    log_sink = LogSink(store, job.id, bot, admin_chat_id)

    async with telethon_client(account.telethon_session) as client:
        status = await schedule_one_chat(
            store,
            client,
            acc,
            chat,
            posts,
            log_sink,
            skip_abandoned=False,
            is_premium=acc.has_premium,
        )

    ok = status == "ok"
    msg = (
        f"{account.label}: переназначил schedule «{chat.display_name}» → {status}"
    )
    await store.finish_job(job.id, "done" if ok else "error", msg)
    if ok:
        for kind in ("not_assigned", "schedule_empty", "schedule_low"):
            await store.resolve_incidents_matching(
                kind=kind, account_id=account.id, chat_pk=chat.id
            )
    return FixResult(finding.kind, account.id, chat.id, ok, msg)
