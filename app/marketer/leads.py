"""Сбор входящих ЛС (лиды) по Telethon-аккаунтам."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.models import Account
from app.reporting.stats import local_day
from app.store import Store
from app.tg.client import telethon_client

log = logging.getLogger("marketing.marketer.leads")


@dataclass
class LeadScanResult:
    account_id: int
    new_leads: int = 0
    messages: int = 0
    total_known: int = 0
    error: str = ""
    findings: list = field(default_factory=list)


async def scan_account_leads(store: Store, account: Account) -> LeadScanResult:
    """Уникальные люди в ЛС + сообщения за сегодня."""
    result = LeadScanResult(account_id=account.id)
    if not account.telethon_session:
        return result

    settings = get_settings()
    day = local_day(settings.timezone)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    local_midnight = (
        datetime.now(ZoneInfo(settings.timezone))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )

    messages_today = 0

    try:
        async with telethon_client(account.telethon_session) as client:
            async for dialog in client.iter_dialogs(limit=200):
                if not getattr(dialog, "is_user", False):
                    continue
                entity = dialog.entity
                if getattr(entity, "bot", False) or getattr(entity, "is_self", False):
                    continue
                user_id = int(getattr(entity, "id", 0) or 0)
                if not user_id:
                    continue
                username = (getattr(entity, "username", None) or "")[:64]

                try:
                    msgs = await client.get_messages(entity, limit=10)
                except Exception:
                    continue
                incoming = [m for m in msgs if m and not getattr(m, "out", False)]
                if not incoming:
                    continue

                today_in = 0
                newest_iso = now_iso
                for m in incoming:
                    md = m.date
                    if md is None:
                        continue
                    if md.tzinfo is None:
                        md = md.replace(tzinfo=timezone.utc)
                    if md >= local_midnight:
                        today_in += 1
                        newest_iso = md.isoformat(timespec="seconds")

                # Всегда регистрируем лида, если писал когда-либо
                last_m = incoming[0].date
                if last_m and last_m.tzinfo is None:
                    last_m = last_m.replace(tzinfo=timezone.utc)
                seen = (
                    newest_iso
                    if today_in
                    else (
                        last_m.isoformat(timespec="seconds") if last_m else now_iso
                    )
                )
                await store.upsert_lead(
                    account_id=account.id,
                    user_id=user_id,
                    username=username,
                    seen_at=seen,
                    add_messages=0,
                )
                messages_today += today_in

    except Exception as e:
        log.warning("leads scan failed acc=%s: %s", account.id, e)
        result.error = f"{type(e).__name__}: {e}"
        return result

    new_today = await store.count_leads_since(account.id, day)
    await store.set_lead_stat(
        day=day,
        account_id=account.id,
        new_leads=new_today,
        messages=messages_today,
    )

    result.new_leads = new_today
    result.messages = messages_today
    result.total_known = await store.count_leads(account.id)
    return result
