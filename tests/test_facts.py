from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import get_settings
from app.store import Store
from app.ui.facts_view import day_view, hour_view, last_hour_view
from app.utils.chat_ids import canon_chat_id
from app.utils.coverage import accounts_needed, max_gap
from app.utils.facts import hour_counts, last_hour_window, minute_cells, window_utc

TZ = ZoneInfo("Asia/Bishkek")


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "facts.db")
    await db.init()
    return db


def test_canon_chat_id():
    assert canon_chat_id("-1001234567890") == "1234567890"
    assert canon_chat_id(1234567890) == "1234567890"
    assert canon_chat_id("-4012345") == "4012345"
    assert canon_chat_id("@Lustify") == "lustify"


def test_last_hour_window_rows_are_unique_minutes():
    now = datetime(2026, 10, 2, 14, 23, 40, tzinfo=TZ)
    start = last_hour_window(now, TZ)
    assert start == datetime(2026, 10, 2, 13, 24, tzinfo=TZ)
    ev = []
    for hh, mm in ((13, 30), (14, 5), (14, 23)):
        ev.append((1, 7, datetime(2026, 10, 2, hh, mm, 10, tzinfo=TZ).astimezone(timezone.utc).isoformat()))
    # вне окна
    ev.append((1, 7, datetime(2026, 10, 2, 13, 10, tzinfo=TZ).astimezone(timezone.utc).isoformat()))
    cells = minute_cells(ev, start, 60, TZ)
    assert sorted(cells) == [(6, 7), (41, 7), (59, 7)]


def test_hour_counts():
    base = datetime(2026, 10, 1, 0, 0, tzinfo=TZ)
    ev = [
        (1, 3, datetime(2026, 10, 1, 8, 7, tzinfo=TZ).astimezone(timezone.utc).isoformat()),
        (2, 3, datetime(2026, 10, 1, 8, 50, tzinfo=TZ).astimezone(timezone.utc).isoformat()),
        (2, 4, datetime(2026, 10, 1, 23, 59, tzinfo=TZ).astimezone(timezone.utc).isoformat()),
        (2, 4, datetime(2026, 10, 2, 0, 1, tzinfo=TZ).astimezone(timezone.utc).isoformat()),
    ]
    c = hour_counts(ev, base, TZ)
    assert c[(8, 3)] == 2 and c[(23, 4)] == 1 and len(c) == 2


def test_coverage_math():
    assert accounts_needed(60) == 12
    assert accounts_needed(30) == 6
    assert max_gap([0, 5, 16, 23, 29, 34, 39, 43, 57]) == 14
    assert max_gap([]) == 60


async def test_store_events_idempotent(store: Store):
    acc = await store.add_account("a")
    chat = await store.add_chat("Get-CHAT УСЛУГИ", "-1001", kind="schedule")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    row = (acc.id, chat.id, now.isoformat(timespec="seconds"), 55)
    assert await store.add_send_events([row]) == 1
    assert await store.add_send_events([row]) == 0
    got = await store.send_events_between(
        (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
        (now + timedelta(minutes=1)).isoformat(timespec="seconds"),
    )
    assert len(got) == 1


async def test_views_render(store: Store, tmp_path: Path):
    a1 = await store.add_account("alex")
    a2 = await store.add_account("pablo")
    get_chat = await store.add_chat("Get-CHAT УСЛУГИ", "-1001", kind="schedule")
    other = await store.add_chat("EQL | ЧАТ", "-1002", kind="schedule")
    now = datetime.now(timezone.utc)
    rows = [
        (a1.id, get_chat.id, (now - timedelta(minutes=10)).isoformat(timespec="seconds"), 1),
        (a2.id, get_chat.id, (now - timedelta(minutes=30)).isoformat(timespec="seconds"), 2),
    ]
    await store.add_send_events(rows)
    text, png = await last_hour_view(store)
    assert "Get-CHAT" in text and "отправок: 2" in text or "Всего: <b>2</b>" in text
    assert png and png[:4] == b"\x89PNG"
    (tmp_path / "hour.png").write_bytes(png)

    text, png, per_hour = await day_view(store, 0)
    assert sum(per_hour) == 2 and png
    text, png = await hour_view(store, 0, datetime.now(TZ).hour)
    assert png


async def test_abandoned_counter_does_not_grow(store: Store):
    acc = await store.add_account("a")
    chat = await store.add_chat("Casino", "-1009", kind="schedule")
    for _ in range(3):
        await store.record_setup_fail(acc.id, chat.id, "x", max_attempts=3)
    for _ in range(20):
        st = await store.record_setup_fail(acc.id, chat.id, "x", max_attempts=3)
    assert st.status == "abandoned" and st.fail_count == 3


async def test_restrictions_and_prefs(store: Store):
    acc = await store.add_account("a")
    chat = await store.add_chat("Ogma", "-1001000000003", kind="schedule")
    r = await store.upsert_restriction(acc.id, chat.id, "mute", reason="rule 3", until_at="2026-10-05T00:00:00+00:00")
    assert r.is_mute and r.reason == "rule 3"
    assert await store.banned_pairs() == set()
    await store.upsert_restriction(acc.id, chat.id, "ban", error="UserBannedInChannelError")
    assert await store.banned_pairs() == {(acc.id, chat.id)}
    assert await store.resolve_restriction(acc.id, chat.id)
    assert await store.list_restrictions() == []

    pref = await store.get_pref(acc.id, "-1001000000003")
    assert pref.is_enabled and pref.mention == -1
    await store.set_pref(acc.id, "-1001000000003", enabled=False)
    await store.set_pref(acc.id, 1000000003, mention=1)  # тот же чат, другой формат id
    pref = await store.get_pref(acc.id, "-1001000000003")
    assert not pref.is_enabled and pref.mention == 1
    assert (acc.id, "1000000003") in await store.disabled_pairs()

    assert await store.get_chat_post(acc.id, "-1001000000003") is None
    await store.save_chat_post(acc.id, "-1001000000003", "строго по форме", [], "")
    post = await store.get_chat_post(acc.id, "-1001000000003")
    assert post and post.text == "строго по форме"
