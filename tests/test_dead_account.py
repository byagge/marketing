from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.jobs import setup as setup_mod
from app.store import Store
from app.utils.schedule import (
    build_schedule_times,
    dead_interval,
    phase_offset_minutes,
)

TZ = ZoneInfo("Asia/Bishkek")


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "dead.db")
    await db.init()
    return db


# ---------- store ----------


async def test_dead_flag_default_and_toggle(store: Store):
    acc = await store.add_account("a1")
    assert acc.is_dead == 0 and not acc.dead
    acc = await store.update_account(acc.id, is_dead=1)
    assert acc.dead
    acc = await store.update_account(acc.id, is_dead=0)
    assert not acc.dead


async def test_delete_slots_for_account(store: Store):
    a1 = await store.add_account("a1")
    a2 = await store.add_account("a2")
    chat = await store.add_chat("C", "-1001", kind="schedule")
    await store.set_slot(chat.id, a1.id, 5)
    await store.set_slot(chat.id, a2.id, 10)
    assert await store.delete_slots_for_account(a1.id) == 1
    assert [s.account_id for s in await store.slots_for_chat(chat.id)] == [a2.id]


async def test_dead_account_excluded_from_retries(store: Store):
    live = await store.add_account("live")
    dead = await store.add_account("dead")
    await store.update_account(dead.id, is_dead=1)
    chat = await store.add_chat("C", "-1001", kind="schedule")
    await store.record_setup_fail(live.id, chat.id, "x", max_attempts=3)
    await store.record_setup_fail(dead.id, chat.id, "x", max_attempts=3)
    due = await store.list_due_setup_retries(
        retry_days=0, max_attempts=3, now=datetime(2030, 1, 1, tzinfo=ZoneInfo("UTC"))
    )
    assert {s.account_id for s in due} == {live.id}


# ---------- schedule math ----------


def test_dead_interval_bounds():
    # не плотнее, чем 99 слотов на сутки (≈14 мин)
    assert dead_interval(60, 15) == 15
    assert dead_interval(60, 5) == 14
    # чат и так чаще — берём чат
    assert dead_interval(20, 30) == 20
    assert dead_interval(10, 15) == 14


def test_phase_offset_only_for_long_intervals():
    assert phase_offset_minutes(3, 60) == 0
    assert phase_offset_minutes(3, 30) == 0
    # 6 часов → 6 фаз по часу
    assert [phase_offset_minutes(r, 360) for r in range(7)] == [
        0, 60, 120, 180, 240, 300, 0,
    ]


def test_long_interval_spreads_accounts_over_the_day():
    now = datetime(2026, 9, 8, 8, 0, tzinfo=TZ)
    hours = set()
    for rank in range(6):
        times = build_schedule_times(
            10,
            360,
            now=now,
            tz=TZ,
            start_hour=9,
            offset_minutes=phase_offset_minutes(rank, 360),
        )
        hours.add(times[0].hour)
    # без сдвига все шесть стартовали бы в 09:10
    assert hours == {9, 10, 11, 12, 13, 14}


# ---------- run_dead_schedule ----------


class _FakeClient:
    def __init__(self, entities):
        self._entities = entities

    async def get_me(self):
        return SimpleNamespace(premium=False)

    async def iter_dialogs(self):
        for ent in self._entities:
            yield SimpleNamespace(entity=ent)


async def test_dead_schedule_posts_everywhere_available_quietly(
    store: Store, monkeypatch
):
    acc = await store.add_account("dead1")
    await store.update_account(acc.id, is_dead=1, telethon_session="x.session")
    await store.save_post(acc.id, "ru", "hello", [])
    member = await store.add_chat("Member", "-1001000000001", kind="schedule")
    absent = await store.add_chat("Absent", "-1001000000002", kind="schedule")
    by_name = await store.add_chat(
        "ByName", "-1001000000003", username="byname", kind="schedule"
    )

    @asynccontextmanager
    async def fake_client(_session):
        yield _FakeClient(
            [
                SimpleNamespace(id=1000000001, username=None),
                SimpleNamespace(id=555, username="ByName"),
            ]
        )

    calls = []

    async def fake_schedule_chat_posts(client, target, **kwargs):
        calls.append((target, kwargs))
        return {
            "title": "t", "peer_id": 1, "cleared": 0, "planned": 92,
            "success": 92, "error": "", "first_time": "", "start_minute": 0,
            "media_blocked": False, "used_photo": False, "mode": "post",
        }

    monkeypatch.setattr(setup_mod, "telethon_client", fake_client)
    monkeypatch.setattr(setup_mod, "schedule_chat_posts", fake_schedule_chat_posts)
    monkeypatch.setattr(setup_mod, "peer_id", lambda e: -1000000000000 - e.id)

    buckets = await setup_mod.run_dead_schedule(store, acc.id, None, None)

    assert sorted(buckets["ok"]) == ["ByName", "Member"]
    assert buckets["skipped"] == ["Absent"]
    assert len(calls) == 2
    for _target, kw in calls:
        assert kw["interval_minutes"] == 15  # dead_interval_minutes по умолчанию
        assert kw["offset_minutes"] == 0
    # ни слотов, ни setup_states: расходник не ломает сетку и статистику отказов
    assert await store.slots_for_chat(member.id) == []
    assert await store.list_setup_states(acc.id) == []
    assert absent.id and by_name.id


async def test_dead_chat_errors_do_not_count_as_failures(store: Store, monkeypatch):
    acc = await store.add_account("dead2")
    await store.update_account(acc.id, is_dead=1, telethon_session="x.session")
    await store.save_post(acc.id, "ru", "hello", [])
    chat = await store.add_chat("Banned", "-1001000000001", kind="schedule")

    @asynccontextmanager
    async def fake_client(_session):
        yield _FakeClient([SimpleNamespace(id=1000000001, username=None)])

    async def boom(*_a, **_kw):
        raise RuntimeError("ChatWriteForbiddenError")

    monkeypatch.setattr(setup_mod, "telethon_client", fake_client)
    monkeypatch.setattr(setup_mod, "schedule_chat_posts", boom)
    monkeypatch.setattr(setup_mod, "peer_id", lambda e: -1000000000000 - e.id)

    for _ in range(5):  # больше max_attempts — «abandoned» быть не должно
        buckets = await setup_mod.run_dead_schedule(store, acc.id, None, None)
        assert buckets["skipped"] == ["Banned"]
    assert await store.list_setup_states(acc.id) == []
    assert chat.id


async def test_live_account_gets_phase_offset_on_long_interval(
    store: Store, monkeypatch
):
    accounts = []
    for i in range(3):
        a = await store.add_account(f"a{i}")
        await store.save_post(a.id, "ru", "hello", [])
        accounts.append(a)
    chat = await store.add_chat(
        "SA", "-1001000000001", kind="schedule", interval_minutes=360
    )
    for a, minute in zip(accounts, (40, 10, 25)):
        await store.set_slot(chat.id, a.id, minute)

    seen = {}

    async def fake_schedule_chat_posts(client, target, **kwargs):
        seen[kwargs["start_minute"]] = kwargs["offset_minutes"]
        return {
            "title": "t", "peer_id": 1, "cleared": 0, "planned": 4,
            "success": 4, "error": "", "first_time": "", "start_minute": 0,
            "media_blocked": False, "used_photo": False, "mode": "post",
        }

    monkeypatch.setattr(setup_mod, "schedule_chat_posts", fake_schedule_chat_posts)
    chat = await store.get_chat(chat.id)
    for a in accounts:
        posts = {"ru": await store.get_post(a.id, "ru")}
        out = await setup_mod.schedule_one_chat(
            store,
            None,
            a,
            chat,
            posts,
            setup_mod.LogSink(store, (await store.create_job("t", a.id)).id),
            entity=SimpleNamespace(),
        )
        assert out == "ok"
    # ранги по минуте: 10→0, 25→1, 40→2 → сдвиг 0/60/120 мин
    assert seen == {10: 0, 25: 60, 40: 120}
