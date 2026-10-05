from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telethon.tl import functions, types

from app.jobs import setup as setup_mod
from app.store import Store
from app.tg.scheduler import reconcile_scheduled, schedule_chat_posts
from app.utils.schedule import build_schedule_times

TZ = ZoneInfo("Asia/Bishkek")


@pytest.fixture(autouse=True)
def _fast_pauses(monkeypatch):
    from app.config import get_settings

    st = get_settings()
    monkeypatch.setattr(st, "schedule_pause_sec", 0.0001)
    monkeypatch.setattr(st, "schedule_delete_pause_sec", 0.0001)


class FakeTelegram:
    """Минимальный клиент: хранит scheduled одного чата и считает вызовы."""

    def __init__(self):
        self.msgs: list[SimpleNamespace] = []
        self.next_id = 1
        self.created = 0
        self.deleted = 0

    async def get_input_entity(self, e):
        return e

    async def __call__(self, req):
        if isinstance(req, functions.messages.GetScheduledHistoryRequest):
            return SimpleNamespace(messages=list(self.msgs))
        if isinstance(req, functions.messages.DeleteScheduledMessagesRequest):
            ids = set(req.id)
            self.deleted += len(ids & {m.id for m in self.msgs})
            self.msgs = [m for m in self.msgs if m.id not in ids]
            return SimpleNamespace()
        if isinstance(req, functions.messages.SendMessageRequest):
            m = SimpleNamespace(id=self.next_id, date=req.schedule_date)
            self.next_id += 1
            self.created += 1
            self.msgs.append(m)
            return SimpleNamespace(updates=[SimpleNamespace(message=m)])
        raise AssertionError(f"unexpected {type(req).__name__}")


PEER = types.PeerChannel(channel_id=1001)
KW = dict(
    text="hi", entities=None, start_minute=7, interval_minutes=60,
    start_hour=9, tz=TZ, repeat_period=None, pause=0, 
)


async def _build(client, **extra):
    return await schedule_chat_posts(client, PEER, **{**KW, **extra})


async def test_first_build_then_noop_sync():
    tg = FakeTelegram()
    first = await _build(tg)
    assert first["success"] == first["planned"] == 24
    assert tg.created == 24

    tg.created = tg.deleted = 0
    again = await _build(tg, sync=True)
    assert again["kept"] == 24
    assert tg.created == 0 and tg.deleted == 0  # ночью ничего не пересоздаём
    assert again["success"] == again["planned"]


async def test_sync_fills_gaps_and_drops_stale():
    tg = FakeTelegram()
    await _build(tg)
    # потеряли 3 слота + появился лишний на «чужое» время
    tg.msgs = tg.msgs[3:]
    tg.msgs.append(SimpleNamespace(id=999, date=datetime(2030, 1, 1, tzinfo=timezone.utc)))
    tg.created = tg.deleted = 0
    res = await _build(tg, sync=True)
    assert tg.created == 3 and tg.deleted == 1
    assert res["success"] == res["planned"] == 24
    assert len(tg.msgs) == 24


async def test_replace_mode_rebuilds_everything():
    tg = FakeTelegram()
    await _build(tg)
    tg.created = tg.deleted = 0
    await _build(tg, sync=False)
    assert tg.created == 24 and tg.deleted == 24


async def test_reconcile_reports_missing_and_stale():
    tg = FakeTelegram()
    times = build_schedule_times(5, 60, tz=TZ, start_hour=9)
    for i, t in enumerate(times[:10]):
        tg.msgs.append(SimpleNamespace(id=i + 1, date=t))
    missing, stale, kept = await reconcile_scheduled(tg, PEER, times)
    assert kept == 10 and len(missing) == 14 and stale == []


# ---------- решение sync в schedule_one_chat ----------


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "sync.db")
    await db.init()
    return db


async def _run_one(store, acc, chat, monkeypatch, sync):
    seen = {}

    async def fake_posts(client, target, **kw):
        seen["sync"] = kw["sync"]
        return {
            "title": "t", "peer_id": 1, "cleared": 0, "kept": 0, "planned": 24,
            "success": 24, "error": "", "first_time": "", "start_minute": 0,
            "media_blocked": False, "used_photo": False, "mode": "post",
        }

    monkeypatch.setattr(setup_mod, "schedule_chat_posts", fake_posts)
    log = setup_mod.LogSink(store, (await store.create_job("t", acc.id)).id)
    posts = {"ru": await store.get_post(acc.id, "ru")}
    out = await setup_mod.schedule_one_chat(
        store, None, acc, chat, posts, log, entity=SimpleNamespace(), sync=sync
    )
    assert out == "ok"
    return seen["sync"]


async def test_sync_only_when_config_unchanged(store, monkeypatch):
    acc = await store.add_account("a")
    await store.save_post(acc.id, "ru", "hello", [])
    chat = await store.add_chat("C", "-1001", kind="schedule")
    await store.set_slot(chat.id, acc.id, 12)

    # первая сборка — полная (нет sig)
    assert await _run_one(store, acc, chat, monkeypatch, sync=True) is False
    # настройки те же → доливаем
    assert await _run_one(store, acc, chat, monkeypatch, sync=True) is True
    # без sync-режима (ручной Старт) — всегда полная пересборка
    assert await _run_one(store, acc, chat, monkeypatch, sync=False) is False
    # поменяли текст поста → полная пересборка, потом снова sync
    await store.save_post(acc.id, "ru", "NEW TEXT", [])
    assert await _run_one(store, acc, chat, monkeypatch, sync=True) is False
    assert await _run_one(store, acc, chat, monkeypatch, sync=True) is True
    # сменили минуту (перераспределение) → полная
    await store.set_slot(chat.id, acc.id, 31)
    assert await _run_one(store, acc, chat, monkeypatch, sync=True) is False


async def test_first_ok_at_is_sticky(store):
    acc = await store.add_account("a")
    chat = await store.add_chat("C", "-1001", kind="schedule")
    s1 = await store.record_setup_ok(acc.id, chat.id, "x")
    s2 = await store.record_setup_ok(acc.id, chat.id, "x")
    assert s1.first_ok_at == s2.first_ok_at != ""
    await store.record_setup_fail(acc.id, chat.id, "boom", max_attempts=3)
    s3 = await store.record_setup_ok(acc.id, chat.id, "y")
    assert s3.first_ok_at >= s1.first_ok_at and s3.sig == "y"


async def test_nonpremium_horizon_extends_but_premium_does_not():
    tg = FakeTelegram()
    res = await _build(tg, extra_hours=6)
    assert 24 < len(tg.msgs) <= 30 and res["success"] == len(tg.msgs)
    assert len({m.date for m in tg.msgs}) == len(tg.msgs)

    tg2 = FakeTelegram()
    await _build(tg2, extra_hours=6, repeat_period=86400)
    assert len(tg2.msgs) == 24  # repeat Premium: горизонт не нужен


async def test_horizon_never_exceeds_telegram_limit():
    tg = FakeTelegram()
    await _build(tg, interval_minutes=15, extra_hours=6)  # 90 слотов + extra > 99 → без extra
    assert len(tg.msgs) == 90


async def test_fact_scheduled_count_roundtrip(store):
    from app.utils.balance import PairFact

    acc = await store.add_account("a")
    chat = await store.add_chat("C", "-1001", kind="schedule")
    await store.upsert_fact(PairFact(acc.id, chat.id, member=1, scheduled_count=17))
    [f] = await store.list_facts()
    assert f.scheduled_count == 17
    await store.upsert_fact(PairFact(acc.id, chat.id, member=1))
    [f] = await store.list_facts()
    assert f.scheduled_count is None
