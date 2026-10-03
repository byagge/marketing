"""Tests for reporting classification, stats, active slots, incidents, density."""

from __future__ import annotations

import pytest

from app.store import Store


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
