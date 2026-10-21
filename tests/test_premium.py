"""Premium expiry helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.store import Store
from app.tg.premium import (
    PremiumInfo,
    _parse_date_in_text,
    needs_warn,
    refresh_account_premium,
)


def test_parse_date_in_text():
    assert _parse_date_in_text("до 15.11.2026").date().isoformat() == "2026-11-15"
    assert _parse_date_in_text("until 2026-12-01").date().isoformat() == "2026-12-01"


def test_needs_warn_window():
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    soon = (now + timedelta(hours=12)).isoformat()
    later = (now + timedelta(days=3)).isoformat()
    assert needs_warn(soon, now=now) is True
    assert needs_warn(later, now=now) is False


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.db")
    await s.init()
    return s


async def test_refresh_sets_estimate_when_new_premium(store: Store):
    acc = await store.add_account("p1")
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)

    class Client:
        async def get_me(self):
            return SimpleNamespace(premium=True)

        async def __call__(self, req):
            return SimpleNamespace(
                status_text="You have Telegram Premium",
                period_options=[SimpleNamespace(current=True, months=1)],
            )

    info = await refresh_account_premium(store, Client(), acc, now=now)
    assert info.is_premium and info.source == "estimate"
    fresh = await store.get_account(acc.id)
    assert fresh.is_premium == 1 and fresh.premium_since and fresh.premium_until


async def test_promo_past_does_not_renew(store: Store):
    acc = await store.add_account("Czech")
    await store.update_account(
        acc.id,
        is_premium=1,
        premium_since="2026-09-19T00:00:00+00:00",
        premium_until="2026-10-19T00:00:00+00:00",
        premium_until_source="promo",
    )
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    class Client:
        async def get_me(self):
            return SimpleNamespace(premium=True)

        async def __call__(self, req):
            return SimpleNamespace(status_text="Telegram Premium", period_options=[])

    acc = await store.get_account(acc.id)
    info = await refresh_account_premium(store, Client(), acc, now=now)
    assert info.is_premium is False
    assert info.source == "expired"
    fresh = await store.get_account(acc.id)
    assert fresh.is_premium == 0


async def test_expire_lapsed_clears_flag(store: Store):
    from app.tg.premium import expire_lapsed_premium

    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    promo = await store.add_account("Death")
    await store.update_account(
        promo.id,
        is_premium=1,
        premium_until="2026-10-19T00:00:00+00:00",
        premium_until_source="promo",
    )
    estimate = await store.add_account("black")
    await store.update_account(
        estimate.id,
        is_premium=1,
        premium_until="2026-10-01T00:00:00+00:00",
        premium_until_source="estimate",
    )
    ended = await expire_lapsed_premium(store, now=now)
    assert ended == ["Death"]
    assert (await store.get_account(estimate.id)).is_premium == 1

