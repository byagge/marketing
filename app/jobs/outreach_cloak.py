"""Клоакинг аутрич-аккаунтов: маркетолог отвечает в ЛС сам (sender не используется)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.jobs.facts import dialog_entities
from app.store import Store
from app.tg.client import telethon_client
from app.tg.outreach_cloak import (
    CloakAccountResult,
    collect_admin_ids,
    process_outreach_cloak,
)
from app.tg.sender_push import normalize_multiline
from app.utils.errfmt import short_error

log = logging.getLogger("marketing.jobs.outreach_cloak")


@dataclass
class OutreachCloakSummary:
    accounts: int = 0
    sent: int = 0
    skipped_admin: int = 0
    skipped_already: int = 0
    errors: list[str] = field(default_factory=list)
    per_account: list[CloakAccountResult] = field(default_factory=list)

    def line(self) -> str:
        if not self.accounts:
            return "outreach_cloak: нет аутрич-аккаунтов или клоакинг выкл"
        err_n = len(self.errors)
        return (
            f"outreach_cloak: accounts={self.accounts} sent={self.sent} "
            f"admin={self.skipped_admin} already={self.skipped_already} err={err_n}"
        )


async def run_outreach_cloak(store: Store) -> OutreachCloakSummary:
    """Пройти аутрич-аккаунты и отправить клоакинг тем, кто написал первым."""
    summary = OutreachCloakSummary()
    ss = await store.sender_settings()
    text = normalize_multiline(ss.cloak_text)
    if not (ss.cloak_enabled and text.strip()):
        return summary

    accounts = [a for a in await store.list_accounts() if a.is_outreach and a.telethon_session]
    if not accounts:
        return summary

    chats = await store.list_chats()
    for acc in accounts:
        summary.accounts += 1
        try:
            async with telethon_client(acc.telethon_session) as client:
                # Сначала обновим индекс ЛС (кто написал первым).
                from app.jobs.dms import collect_dms

                await collect_dms(store, client, acc)
                entities_map = await dialog_entities(client, chats) if chats else {}
                ents = list(entities_map.values())
                admins = await collect_admin_ids(client, ents)
                one = await process_outreach_cloak(
                    store,
                    client,
                    acc,
                    chats,
                    cloak_text=text,
                    cloak_entities_json=ss.cloak_entities_json,
                    admin_ids=admins,
                    chat_entities=ents,
                )
        except Exception as e:  # noqa: BLE001
            err = f"{acc.label}: {short_error(e)}"
            summary.errors.append(err)
            log.warning("outreach cloak account failed: %s", err)
            continue

        summary.per_account.append(one)
        summary.sent += one.sent
        summary.skipped_admin += one.skipped_admin
        summary.skipped_already += one.skipped_already
        summary.errors.extend(f"{acc.label}: {e}" for e in one.errors)

    if summary.sent or summary.errors:
        log.info("%s", summary.line())
    return summary
