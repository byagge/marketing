"""Статус Telegram Premium: флаг + оценка срока окончания.

У User нет поля expires. Берём:
- get_me().premium;
- help.getPremiumPromo (status_text + текущий period_options.months);
- если Premium только что появился — фиксируем since и until ≈ since+months.

Предупреждение «заканчивается за ≤24 ч» — в jobs/premium_watch.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon.tl import functions

from app.models import Account
from app.store import Store
from app.utils.timefmt import parse_utc, to_iso

log = logging.getLogger("marketing.premium")

DEFAULT_MONTHS = 1
WARN_HOURS = 24

# даты в status_text: 10.10.2026 / 2026-10-10 / 10 Oct 2026
_DATE_RX = re.compile(
    r"(?P<d>\d{1,2})[./](?P<m>\d{1,2})[./](?P<y>\d{4})"
    r"|(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})"
)


@dataclass
class PremiumInfo:
    is_premium: bool
    since: str = ""
    until: str = ""
    source: str = ""  # promo|estimate|kept|cleared
    months: int = 0
    status_text: str = ""


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_date_in_text(text: str) -> datetime | None:
    if not text:
        return None
    m = _DATE_RX.search(text)
    if not m:
        return None
    try:
        if m.group("y"):
            d, mo, y = int(m.group("d")), int(m.group("m")), int(m.group("y"))
        else:
            d, mo, y = int(m.group("d2")), int(m.group("m2")), int(m.group("y2"))
        return datetime(y, mo, d, tzinfo=timezone.utc)
    except ValueError:
        return None


def hours_left(until_iso: str, *, now: datetime | None = None) -> float | None:
    until = parse_utc(until_iso)
    if until is None:
        return None
    now = _aware(now or datetime.now(timezone.utc))
    return (until - now).total_seconds() / 3600.0


def needs_warn(until_iso: str, *, now: datetime | None = None) -> bool:
    left = hours_left(until_iso, now=now)
    return left is not None and 0 < left <= WARN_HOURS


def promo_lapsed(account: Account, *, now: datetime | None = None) -> bool:
    """Срок из promo уже в прошлом. Оценку estimate не трогаем — её продлевает probe."""
    if not account.is_premium or not (account.premium_until or "").strip():
        return False
    src = (account.premium_until_source or "").strip()
    if src.startswith("estimate"):
        return False
    left = hours_left(account.premium_until, now=now)
    return left is not None and left <= 0


async def expire_lapsed_premium(store: Store, *, now: datetime | None = None) -> list[str]:
    """Снять is_premium, если premium_until уже прошёл. Возвращает метки аккаунтов."""
    now = _aware(now or datetime.now(timezone.utc))
    ended: list[str] = []
    for acc in await store.list_accounts():
        if not promo_lapsed(acc, now=now):
            continue
        await store.update_account(acc.id, is_premium=0, premium_until_source="expired")
        ended.append(acc.label)
    return ended


async def probe_premium(client: Any) -> PremiumInfo:
    me = await client.get_me()
    is_prem = bool(getattr(me, "premium", False))
    info = PremiumInfo(is_premium=is_prem)
    if not is_prem:
        return info
    try:
        promo = await client(functions.help.GetPremiumPromoRequest())
    except Exception as e:  # noqa: BLE001
        log.debug("getPremiumPromo failed: %s", e)
        return info
    info.status_text = (getattr(promo, "status_text", None) or "").strip()
    parsed = _parse_date_in_text(info.status_text)
    if parsed is not None:
        info.until = to_iso(parsed)
        info.source = "promo"
    months = DEFAULT_MONTHS
    for opt in getattr(promo, "period_options", None) or []:
        if getattr(opt, "current", None):
            months = max(1, int(getattr(opt, "months", 0) or DEFAULT_MONTHS))
            break
    else:
        opts = list(getattr(promo, "period_options", None) or [])
        if opts:
            months = max(1, int(getattr(opts[0], "months", 0) or DEFAULT_MONTHS))
    info.months = months
    return info


async def refresh_account_premium(
    store: Store,
    client: Any,
    account: Account,
    *,
    now: datetime | None = None,
) -> PremiumInfo:
    """Обновить is_premium / since / until в БД по живому клиенту."""
    now = _aware(now or datetime.now(timezone.utc))
    probed = await probe_premium(client)
    prev_on = bool(account.is_premium)
    fields: dict[str, Any] = {"is_premium": 1 if probed.is_premium else 0}

    if not probed.is_premium:
        if prev_on or account.premium_until or account.premium_since:
            fields["premium_since"] = ""
            fields["premium_until"] = ""
            fields["premium_until_source"] = "cleared"
            fields["premium_warned_at"] = ""
            probed.source = "cleared"
        await store.update_account(account.id, **fields)
        return probed

    since = account.premium_since or ""
    until = account.premium_until or ""
    source = account.premium_until_source or ""

    if probed.until:
        until = probed.until
        source = "promo"
    elif not since:
        since = to_iso(now)
        months = probed.months or DEFAULT_MONTHS
        until = to_iso(now + timedelta(days=30 * months))
        source = "estimate"
        probed.months = months
    elif not until:
        base = parse_utc(since) or now
        months = probed.months or DEFAULT_MONTHS
        until = to_iso(base + timedelta(days=30 * months))
        source = "estimate"
        probed.months = months
    else:
        # until в прошлом и новой даты из promo нет.
        # Промо-Premium так и зависал: get_me().premium ещё true, а срок уже вышел,
        # и старая ветка рисовала новое until. Оценку (estimate) можно продлить,
        # дату из promo — нет.
        u = parse_utc(until)
        src = (source or "").strip()
        promo_lapsed = u is not None and u <= now and not src.startswith("estimate")
        if promo_lapsed:
            fields["is_premium"] = 0
            fields["premium_until_source"] = "expired"
            await store.update_account(account.id, **fields)
            probed.is_premium = False
            probed.since = since
            probed.until = until
            probed.source = "expired"
            return probed
        if u is not None and u <= now:
            months = probed.months or DEFAULT_MONTHS
            until = to_iso(now + timedelta(days=30 * months))
            source = "estimate_renew"
            probed.months = months
        else:
            source = source or "kept"

    if not since:
        since = to_iso(now)

    fields.update(
        premium_since=since,
        premium_until=until,
        premium_until_source=source,
    )
    await store.update_account(account.id, **fields)
    probed.since = since
    probed.until = until
    probed.source = source
    return probed
