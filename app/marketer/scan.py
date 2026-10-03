"""Сканирование аккаунтов: schedule-очередь, premium, sender last_sent."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import get_settings
from app.models import Account, Chat
from app.reporting.classify import (
    ProblemSpec,
    classify_error,
    classify_health_status,
    classify_or_unknown,
)
from app.reporting.stats import estimate_schedule_sent, hours_between, local_day
from app.sender_api import SenderAPI, SenderAPIError
from app.store import Store
from app.tg.client import telethon_client
from app.tg.health import check_schedule_chats

log = logging.getLogger("marketing.marketer.scan")


@dataclass
class Finding:
    kind: str
    severity: str
    title: str
    detail: str = ""
    account_id: int | None = None
    chat_pk: int | None = None
    auto_fixable: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class AccountScan:
    account_id: int
    label: str
    findings: list[Finding] = field(default_factory=list)
    schedule_rows: list[dict] = field(default_factory=list)
    sent_schedule: float = 0.0
    sent_sender: float = 0.0
    premium_live: bool | None = None


def _finding_from_spec(
    spec: ProblemSpec,
    *,
    account: Account,
    chat: Chat | None = None,
    detail: str = "",
    auto_fixable: bool = False,
) -> Finding:
    return Finding(
        kind=spec.kind,
        severity=spec.severity,
        title=spec.title_ru,
        detail=detail,
        account_id=account.id,
        chat_pk=chat.id if chat else None,
        auto_fixable=auto_fixable,
    )


async def _check_premium(account: Account) -> tuple[bool | None, Finding | None]:
    if not account.telethon_session:
        return None, None
    try:
        async with telethon_client(account.telethon_session) as client:
            me = await client.get_me()
            live = bool(getattr(me, "premium", False))
    except Exception as e:
        return None, Finding(
            kind="session_dead",
            severity="critical",
            title="Telethon session не отвечает",
            detail=f"{type(e).__name__}: {e}",
            account_id=account.id,
            auto_fixable=False,
        )

    if account.has_premium and not live:
        return live, Finding(
            kind="premium_lost",
            severity="high",
            title="Слетела Premium-подписка",
            detail="В БД Premium=да, Telegram говорит нет — daily repeat сломается",
            account_id=account.id,
            auto_fixable=True,  # сбросим флаг is_premium
        )
    if live and not account.has_premium:
        return live, Finding(
            kind="premium_gained",
            severity="info",
            title="Появился Premium",
            detail="Можно включить schedule_repeat — обновлю флаг",
            account_id=account.id,
            auto_fixable=True,
        )
    return live, None


async def _scan_schedule(
    store: Store,
    account: Account,
    chats: list[Chat],
    *,
    is_premium: bool,
) -> tuple[list[Finding], list[dict], float]:
    findings: list[Finding] = []
    sent_total = 0.0
    if not account.telethon_session or not chats:
        return findings, [], 0.0

    chat_by_id = {c.id: c for c in chats}
    slots = await store.all_slots()
    my_slots = [s for s in slots if s.account_id == account.id]
    ok_states = {
        s.chat_pk: s
        for s in await store.list_setup_states(account.id, statuses=["ok"])
    }

    # Проблема №1: в плане есть слот, но факт (setup_ok) нет — сразу в топ
    for slot in my_slots:
        chat = chat_by_id.get(slot.chat_pk)
        if not chat:
            continue
        if slot.chat_pk not in ok_states:
            findings.append(
                Finding(
                    kind="not_assigned",
                    severity="critical",
                    title="Schedule не назначен (нет факта отправки)",
                    detail=(
                        f"В таблице есть минута :{slot.start_minute:02d}, "
                        f"но setup_ok нет — сообщения не уходят"
                    ),
                    account_id=account.id,
                    chat_pk=chat.id,
                    auto_fixable=True,
                )
            )

    try:
        async with telethon_client(account.telethon_session) as client:
            rows = await check_schedule_chats(client, chats)
    except Exception as e:
        findings.append(
            Finding(
                kind="session_dead",
                severity="critical",
                title="Не смог проверить schedule",
                detail=f"{type(e).__name__}: {e}",
                account_id=account.id,
            )
        )
        return findings, [], 0.0

    day = local_day(get_settings().timezone)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for row in rows:
        chat = chat_by_id.get(row["chat_pk"])
        if not chat:
            continue
        prev = await store.latest_health_snapshot(account.id, chat.id)
        hours = hours_between(prev.checked_at, now_iso) if prev else 0.0
        sent_est = estimate_schedule_sent(
            prev_count=prev.count if prev else None,
            curr_count=int(row["count"]),
            expected=int(row["expected"]),
            is_premium=is_premium,
            interval_minutes=chat.interval_minutes,
            hours_elapsed=hours if hours > 0 else 1.0,
        )
        if sent_est > 0:
            await store.add_send_stat(
                day=day,
                account_id=account.id,
                channel="schedule",
                messages=sent_est,
                chat_pk=chat.id,
            )
            sent_total += sent_est

        await store.add_health_snapshot(
            account_id=account.id,
            chat_pk=chat.id,
            status=row["status"],
            count=int(row["count"]),
            expected=int(row["expected"]),
            error=row.get("error") or "",
            sent_est=sent_est,
            checked_at=now_iso,
        )

        had_ok = chat.id in ok_states or (prev is not None and prev.status == "ok")
        # Только по чатам, где есть слот или был ok — иначе шум по «чужим» аккаунтам
        has_slot = any(s.chat_pk == chat.id for s in my_slots)
        if not has_slot and chat.id not in ok_states:
            if row["status"] == "ok":
                await store.resolve_incidents_matching(
                    kind="schedule_empty", account_id=account.id, chat_pk=chat.id
                )
            continue

        spec = classify_health_status(
            row["status"],
            error=row.get("error") or "",
            had_ok_before=had_ok,
        )
        if not spec and row["status"] not in {"ok", ""}:
            spec = classify_or_unknown(
                row.get("error") or row["status"],
                default_title=f"Schedule статус: {row['status']}",
            )
        if spec:
            auto = spec.kind in {"schedule_empty", "schedule_low", "not_assigned"}
            findings.append(
                _finding_from_spec(
                    spec,
                    account=account,
                    chat=chat,
                    detail=(
                        f"{row['count']}/{row['expected']}"
                        + (f" — {row['error']}" if row.get("error") else "")
                    ),
                    auto_fixable=auto,
                )
            )
        else:
            # Здоров — закрываем связанные инциденты
            for kind in (
                "schedule_empty",
                "schedule_low",
                "schedule_error",
                "banned",
                "kicked",
                "not_assigned",
            ):
                await store.resolve_incidents_matching(
                    kind=kind, account_id=account.id, chat_pk=chat.id
                )

    return findings, rows, sent_total


async def _scan_sender(store: Store, account: Account) -> tuple[list[Finding], float]:
    findings: list[Finding] = []
    sent = 0.0
    sender_id = (account.sender_account_id or "").strip()
    if not sender_id:
        return findings, 0.0

    settings = get_settings()
    api = SenderAPI(settings.sender_api_url, settings.sender_api_key)
    try:
        info = await api.get_account(sender_id)
    except SenderAPIError as e:
        findings.append(
            Finding(
                kind="sender_api",
                severity="high",
                title="Autoposter недоступен / аккаунт не найден",
                detail=str(e),
                account_id=account.id,
            )
        )
        return findings, 0.0
    except Exception as e:
        findings.append(
            Finding(
                kind="sender_api",
                severity="medium",
                title="Ошибка опроса sender",
                detail=f"{type(e).__name__}: {e}",
                account_id=account.id,
            )
        )
        return findings, 0.0

    live = bool(info.get("live"))
    spam = bool(info.get("spam") or info.get("spam_task"))
    last_sent = float(info.get("last_sent_at") or 0)
    now_ts = datetime.now(timezone.utc).timestamp()

    if not live:
        findings.append(
            Finding(
                kind="sender_offline",
                severity="high",
                title="Sender-клиент не запущен",
                detail="live=false — рассылка не идёт",
                account_id=account.id,
                auto_fixable=True,
            )
        )
    elif not spam:
        findings.append(
            Finding(
                kind="sender_stopped",
                severity="high",
                title="Sender spam остановлен",
                detail="Аккаунт live, но цикл рассылки не крутится",
                account_id=account.id,
                auto_fixable=True,
            )
        )
    elif last_sent > 0 and (now_ts - last_sent) > 3 * 3600:
        hours = (now_ts - last_sent) / 3600
        findings.append(
            Finding(
                kind="sender_stale",
                severity="critical",
                title="Sender давно не отправлял",
                detail=f"last_sent ~{hours:.1f}ч назад — возможен спам-блок или залипший цикл",
                account_id=account.id,
                auto_fixable=False,
            )
        )
        if hours > 6:
            # Классифицируем как spam_block-подозрение
            findings.append(
                Finding(
                    kind="spam_block",
                    severity="critical",
                    title="Подозрение на спам-блок (sender молчит)",
                    detail=f"Нет отправок {hours:.0f}ч при spam=on",
                    account_id=account.id,
                )
            )

    # Оценка sender-объёма: по активным чатам + last_sent свежести
    try:
        chats = await api.list_chats(sender_id)
    except Exception:
        chats = []

    schedule_chats = await store.list_chats(kind="schedule", enabled_only=True)
    from app.jobs.setup import match_catalog_chat

    active_sender = 0
    recent_sends = 0
    for ch in chats:
        cid = str(ch.get("chat_id") or "")
        if match_catalog_chat(cid, schedule_chats):
            continue
        if not ch.get("active", True):
            continue
        if ch.get("spam_enabled") is False:
            continue
        active_sender += 1
        ls = float(ch.get("last_sent_at") or 0)
        if ls > 0 and (now_ts - ls) < 86400:
            recent_sends += 1

    # Грубая оценка: сколько чатов получили пост за сутки (абсолют, не +=)
    day = local_day(settings.timezone)
    await store.set_send_stat(
        day=day,
        account_id=account.id,
        channel="sender",
        messages=float(recent_sends),
        chat_pk=None,
    )
    sent = float(recent_sends)

    err_text = str(info.get("last_error") or "")
    spec = classify_error(err_text)
    if spec and spec.kind == "spam_block":
        findings.append(
            _finding_from_spec(
                spec,
                account=account,
                detail=err_text[:300],
            )
        )

    return findings, sent


async def scan_account(store: Store, account: Account) -> AccountScan:
    result = AccountScan(account_id=account.id, label=account.label)
    chats = [c for c in await store.list_chats(kind="schedule") if c.enabled]

    premium_live, premium_finding = await _check_premium(account)
    result.premium_live = premium_live
    if premium_finding:
        result.findings.append(premium_finding)

    is_premium = bool(premium_live) if premium_live is not None else account.has_premium
    sch_findings, rows, sch_sent = await _scan_schedule(
        store, account, chats, is_premium=is_premium
    )
    result.findings.extend(sch_findings)
    result.schedule_rows = rows
    result.sent_schedule = sch_sent

    snd_findings, snd_sent = await _scan_sender(store, account)
    result.findings.extend(snd_findings)
    result.sent_sender = snd_sent

    # last_error только если аккаунт в ошибке — иначе вечный шум
    if account.status == "error" and account.last_error:
        spec = classify_or_unknown(account.last_error)
        result.findings.append(
            _finding_from_spec(
                spec,
                account=account,
                detail=account.last_error[:400],
            )
        )

    return result
