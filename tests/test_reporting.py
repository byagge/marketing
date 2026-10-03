"""Tests for reporting classification, stats, active slots, incidents, density."""

from __future__ import annotations

import pytest

from app.models import Account, Chat, MinuteSlot, SetupState
from app.reporting.classify import (
    classify_error,
    classify_health_status,
    classify_or_unknown,
)
from app.reporting.density import avg_gap_minutes, build_density_report, classify_gap
from app.reporting.stats import estimate_schedule_sent, expected_per_chat_24h
from app.store import Store


def test_classify_banned():
    spec = classify_error("UserBannedInChannelError: You're banned from sending")
    assert spec is not None
    assert spec.kind == "banned"
    assert spec.severity == "critical"


def test_classify_premium():
    spec = classify_error("PremiumAccountRequiredError: only for premium")
    assert spec is not None
    assert spec.kind == "premium_lost"


def test_classify_unknown_keeps_all():
    spec = classify_or_unknown("WeirdTellyError: something odd happened")
    assert spec.kind == "unknown"


def test_classify_empty_after_ok():
    spec = classify_health_status("empty", had_ok_before=True)
    assert spec is not None
    assert spec.kind == "schedule_empty"
    assert spec.severity == "critical"


def test_classify_ok_no_problem():
    assert classify_health_status("ok") is None


def test_expected_24h():
    assert expected_per_chat_24h(60) == 24.0
    assert expected_per_chat_24h(30) == 48.0


def test_estimate_nonpremium_drop():
    sent = estimate_schedule_sent(
        prev_count=20,
        curr_count=15,
        expected=23,
        is_premium=False,
        interval_minutes=60,
        hours_elapsed=5,
    )
    assert sent == 5.0


def test_estimate_premium_by_interval():
    sent = estimate_schedule_sent(
        prev_count=23,
        curr_count=23,
        expected=23,
        is_premium=True,
        interval_minutes=60,
        hours_elapsed=12,
    )
    assert sent == pytest.approx(12.0)


def test_density_gap_and_sparse():
    assert avg_gap_minutes(60, 4) == 15.0
    assert avg_gap_minutes(60, 6) == 10.0
    assert avg_gap_minutes(60, 12) == 5.0
    assert classify_gap(15.0) == "sparse"
    assert classify_gap(7.0) == "ok"
    assert classify_gap(3.0) == "dense"


def test_density_report_advice():
    chats = [
        Chat(
            id=1,
            title="G1",
            chat_id="-1001",
            kind="schedule",
            interval_minutes=60,
            enabled=1,
        )
    ]
    accounts = [Account(id=1, label="a1"), Account(id=2, label="a2")]
    slots = [
        MinuteSlot(id=1, chat_pk=1, account_id=1, start_minute=0),
        MinuteSlot(id=2, chat_pk=1, account_id=2, start_minute=30),
    ]
    ok = [
        SetupState(id=1, account_id=1, chat_pk=1, status="ok"),
        SetupState(id=2, account_id=2, chat_pk=1, status="ok"),
    ]
    rep = build_density_report(chats, accounts, slots, ok, target_min=5, target_max=10)
    assert rep.chats[0].avg_gap_min == 30.0
    assert rep.chats[0].status == "sparse"
    assert rep.chats[0].accounts_needed_for_10m >= 4
    assert any("акк" in a.lower() for a in rep.global_advice)


@pytest.mark.asyncio
async def test_active_slots_only_ok(tmp_path):
    store = Store(tmp_path / "t.db")
    await store.init()
    acc = await store.add_account("A1")
    await store.update_account(acc.id, telethon_session="x.session")
    chat = await store.add_chat("Sch", "-1001", kind="schedule")
    await store.set_slot(chat.id, acc.id, 10)
    assert await store.active_slots() == []
    await store.record_setup_ok(acc.id, chat.id)
    active = await store.active_slots()
    assert len(active) == 1
    assert active[0].start_minute == 10


@pytest.mark.asyncio
async def test_incident_upsert_and_resolve(tmp_path):
    store = Store(tmp_path / "inc.db")
    await store.init()
    acc = await store.add_account("A1")
    chat = await store.add_chat("C", "-1002", kind="schedule")
    a = await store.upsert_incident(
        kind="schedule_empty",
        severity="critical",
        title="Пусто",
        account_id=acc.id,
        chat_pk=chat.id,
    )
    b = await store.upsert_incident(
        kind="schedule_empty",
        severity="critical",
        title="Пусто снова",
        detail="0/23",
        account_id=acc.id,
        chat_pk=chat.id,
    )
    assert a.id == b.id
    assert len(await store.list_incidents()) == 1
    n = await store.resolve_incidents_matching(
        kind="schedule_empty", account_id=acc.id, chat_pk=chat.id
    )
    assert n == 1
    assert await store.list_incidents() == []


@pytest.mark.asyncio
async def test_send_stats_and_daily_report(tmp_path):
    store = Store(tmp_path / "st.db")
    await store.init()
    acc = await store.add_account("A1")
    await store.add_send_stat(
        day="2026-09-30", account_id=acc.id, channel="schedule", messages=3, chat_pk=1
    )
    await store.add_send_stat(
        day="2026-09-30", account_id=acc.id, channel="schedule", messages=2, chat_pk=1
    )
    rows = await store.send_stats_for_day("2026-09-30")
    assert rows[0].messages == 5.0
    await store.save_daily_report("2026-09-30", {"schedule_total": 5, "leads_new": 2})
    rep = await store.get_daily_report("2026-09-30")
    assert rep is not None
    assert "leads_new" in rep.payload_json
    assert "2026-09-30" in await store.list_report_days()


@pytest.mark.asyncio
async def test_leads_upsert(tmp_path):
    store = Store(tmp_path / "ld.db")
    await store.init()
    acc = await store.add_account("A1")
    created = await store.upsert_lead(
        account_id=acc.id,
        user_id=42,
        username="u",
        seen_at="2026-09-30T10:00:00+00:00",
    )
    assert created is True
    created2 = await store.upsert_lead(
        account_id=acc.id, user_id=42, username="u", add_messages=2
    )
    assert created2 is False
    assert await store.count_leads(acc.id) == 1
    await store.set_lead_stat(
        day="2026-09-30", account_id=acc.id, new_leads=1, messages=3
    )
    stats = await store.lead_stats_for_day("2026-09-30")
    assert stats[0].new_leads == 1
    assert stats[0].messages == 3
