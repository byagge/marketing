from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.jobs import balance as balance_mod
from app.jobs import maintenance_lock
from app.store import Store
from app.utils.balance import PairFact, max_gap, positions
from app.utils.minutes import even_spaced_minutes


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "bal.db")
    await db.init()
    return db


NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _setup(store: Store, n: int = 12, interval: int = 60):
    chat = await store.add_chat("Lustify", "-1001", kind="schedule", interval_minutes=interval)
    accs = []
    for i, m in enumerate(even_spaced_minutes(60, n)):
        a = await store.add_account(f"a{i}")
        await store.update_account(a.id, telethon_session=f"s{i}.session")
        await store.set_slot(chat.id, a.id, m)
        await store.record_setup_ok(a.id, chat.id)
        accs.append(a)
    return chat, accs


async def _fact(store, a, chat, **kw):
    base = dict(member=1, can_send=1, sent_24h=10, checked_at=NOW)
    base.update(kw)
    await store.upsert_fact(PairFact(a.id, chat.id, **base))


async def test_rebalance_closes_hole_and_reschedules_only_changed(store, monkeypatch):
    chat, accs = await _setup(store, 12)
    # половина аккаунтов (нечётные) вообще не в чате — их минуты были «дырами»
    for i, a in enumerate(accs):
        await _fact(store, a, chat, **({} if i % 2 == 0 else dict(member=0, can_send=0, sent_24h=None)))
    before = {s.account_id: s.start_minute for s in await store.slots_for_chat(chat.id)}
    assert max_gap(positions({a.id: before[a.id] for a in accs[::2]}, 60), 60) >= 10

    calls = []

    async def fake_setup(store_, aid, pks, bot, admin, **kw):
        calls.append((aid, tuple(pks)))
        return {}

    import app.jobs.setup as setup_mod

    monkeypatch.setattr(setup_mod, "run_setup_chats_only", fake_setup)

    report = await balance_mod.run_smart_rebalance(store, collect=False)
    slots = await store.slots_for_chat(chat.id)
    assert {s.account_id for s in slots} == {a.id for a in accs[::2]}
    mins = {s.account_id: s.start_minute for s in slots}
    assert max_gap(positions(mins, 60), 60) == 10  # 60 / 6
    assert len({m for m in mins.values()}) == 6
    assert "не в чате 6" in report
    # пересобираем только тех, у кого минута реально сменилась
    moved = {a for a in mins if mins[a] != before[a]}
    assert {aid for aid, _ in calls} == moved


async def test_dry_run_changes_nothing(store, monkeypatch):
    chat, accs = await _setup(store, 12)
    for i, a in enumerate(accs):
        await _fact(store, a, chat, **({} if i % 2 == 0 else dict(member=0, can_send=0)))
    before = [(s.account_id, s.start_minute) for s in await store.slots_for_chat(chat.id)]
    report = await balance_mod.run_smart_rebalance(store, collect=False, dry_run=True)
    after = [(s.account_id, s.start_minute) for s in await store.slots_for_chat(chat.id)]
    assert before == after
    assert report.startswith("ПЛАН")


async def test_no_facts_means_no_changes(store):
    chat, _ = await _setup(store, 6)
    before = await store.slots_for_chat(chat.id)
    report = await balance_mod.run_smart_rebalance(store, collect=False)
    assert "Нет фактов" in report
    assert len(await store.slots_for_chat(chat.id)) == len(before)


async def test_busy_when_lock_held(store):
    async with maintenance_lock:
        out = await balance_mod.run_smart_rebalance(store, collect=False)
    assert out.startswith("Занято")


async def test_working_member_without_slot_gets_one(store, monkeypatch):
    chat, accs = await _setup(store, 4)
    newbie = await store.add_account("newbie")
    await store.update_account(newbie.id, telethon_session="n.session")
    for a in accs:
        await _fact(store, a, chat)
    await _fact(store, newbie, chat)  # состоит, можно писать, но слота и настройки нет

    calls = []

    async def fake_setup(store_, aid, pks, bot, admin, **kw):
        calls.append(aid)
        return {}

    import app.jobs.setup as setup_mod

    monkeypatch.setattr(setup_mod, "run_setup_chats_only", fake_setup)
    await balance_mod.run_smart_rebalance(store, collect=False)
    assert newbie.id in {s.account_id for s in await store.slots_for_chat(chat.id)}
    assert newbie.id in calls


async def test_pair_with_empty_schedule_is_rebuilt_even_if_state_ok(store, monkeypatch):
    chat, accs = await _setup(store, 6)
    for a in accs:
        await _fact(store, a, chat, scheduled_count=24)
    await _fact(store, accs[2], chat, scheduled_count=0)  # «настроено, но сообщений нет»

    calls = []

    async def fake_setup(store_, aid, pks, bot, admin, **kw):
        calls.append(aid)
        return {}

    import app.jobs.setup as setup_mod

    monkeypatch.setattr(setup_mod, "run_setup_chats_only", fake_setup)
    await balance_mod.run_smart_rebalance(store, collect=False)
    assert calls == [accs[2].id]


async def test_premium_with_legacy_23_slots_is_rebuilt_once(store, monkeypatch):
    chat, accs = await _setup(store, 4)
    await store.update_account(accs[0].id, is_premium=1)
    await store.update_account(accs[1].id, is_premium=0)
    for a in accs:
        await _fact(store, a, chat, scheduled_count=23)  # старая сетка без 24-го слота

    calls = []

    async def fake_setup(store_, aid, pks, bot, admin, **kw):
        calls.append(aid)
        return {}

    import app.jobs.setup as setup_mod

    monkeypatch.setattr(setup_mod, "run_setup_chats_only", fake_setup)
    await balance_mod.run_smart_rebalance(store, collect=False)
    # Premium (repeat) должен иметь 24 → пересобрать; без Premium 23 ≥ половины → не трогаем
    assert calls == [accs[0].id]
