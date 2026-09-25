from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.store import Store
from app.tg.unavailable import is_chat_unavailable


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "setup_states.db")
    await db.init()
    return db


async def test_record_fail_then_abandon(store: Store):
    acc = await store.add_account("a1")
    chat = await store.add_chat("Casino", "-1001", kind="schedule")

    s1 = await store.record_setup_fail(acc.id, chat.id, "missing", max_attempts=3)
    assert s1.status == "pending"
    assert s1.fail_count == 1

    s2 = await store.record_setup_fail(acc.id, chat.id, "missing", max_attempts=3)
    assert s2.status == "pending"
    assert s2.fail_count == 2

    s3 = await store.record_setup_fail(acc.id, chat.id, "missing", max_attempts=3)
    assert s3.status == "abandoned"
    assert s3.fail_count == 3


async def test_record_ok_resets_fail(store: Store):
    acc = await store.add_account("a1")
    chat = await store.add_chat("Casino", "-1001", kind="schedule")
    await store.record_setup_fail(acc.id, chat.id, "missing", max_attempts=3)
    ok = await store.record_setup_ok(acc.id, chat.id)
    assert ok.status == "ok"
    assert ok.fail_count == 0
    assert ok.last_error == ""


async def test_due_retries_respect_days_and_cap(store: Store):
    acc = await store.add_account("a1")
    chat = await store.add_chat("Casino", "-1001", kind="schedule")
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

    await store.record_setup_fail(acc.id, chat.id, "missing", max_attempts=3)
    # Force last_attempt_at into the past
    async with store._connect() as db:
        old = (now - timedelta(days=3)).isoformat(timespec="seconds")
        await db.execute(
            "UPDATE setup_states SET last_attempt_at=? WHERE account_id=? AND chat_pk=?",
            (old, acc.id, chat.id),
        )
        await db.commit()

    due = await store.list_due_setup_retries(retry_days=3, max_attempts=3, now=now)
    assert len(due) == 1
    assert due[0].chat_pk == chat.id

    # Too recent — not due
    await store.record_setup_fail(acc.id, chat.id, "still missing", max_attempts=3)
    due2 = await store.list_due_setup_retries(retry_days=3, max_attempts=3, now=now)
    assert due2 == []

    # Abandoned — not due
    await store.record_setup_fail(acc.id, chat.id, "gone", max_attempts=3)
    state = await store.get_setup_state(acc.id, chat.id)
    assert state is not None and state.is_abandoned
    async with store._connect() as db:
        old = (now - timedelta(days=10)).isoformat(timespec="seconds")
        await db.execute(
            "UPDATE setup_states SET last_attempt_at=? WHERE account_id=? AND chat_pk=?",
            (old, acc.id, chat.id),
        )
        await db.commit()
    due3 = await store.list_due_setup_retries(retry_days=3, max_attempts=3, now=now)
    assert due3 == []


async def test_weekly_reset_reopens_abandoned(store: Store):
    acc = await store.add_account("a1")
    chat = await store.add_chat("Casino", "-1001", kind="schedule")
    for _ in range(3):
        await store.record_setup_fail(acc.id, chat.id, "missing", max_attempts=3)
    state = await store.get_setup_state(acc.id, chat.id)
    assert state is not None and state.is_abandoned

    n = await store.reset_setup_states_for_weekly()
    assert n == 1
    state2 = await store.get_setup_state(acc.id, chat.id)
    assert state2 is not None
    assert state2.status == "pending"
    assert state2.fail_count == 0


async def test_due_includes_zero_fail_after_weekly_reset(store: Store):
    acc = await store.add_account("a1")
    chat = await store.add_chat("Casino", "-1001", kind="schedule")
    for _ in range(3):
        await store.record_setup_fail(acc.id, chat.id, "missing", max_attempts=3)
    await store.reset_setup_states_for_weekly()
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    due = await store.list_due_setup_retries(retry_days=3, max_attempts=3, now=now)
    assert len(due) == 1
    assert due[0].fail_count == 0


def test_is_chat_unavailable_classifies_common_errors():
    assert is_chat_unavailable(ValueError("Cannot find any entity corresponding to"))
    assert is_chat_unavailable(TypeError("Cannot cast InputPeer"))
    assert not is_chat_unavailable(RuntimeError("flood wait weird"))

    from app.tg.unavailable import is_unavailable_text

    banned = (
        "UserBannedInChannelError: You're banned from sending messages "
        "in supergroups/channels (caused by SendMediaRequest)"
    )
    assert is_unavailable_text(banned)
    assert is_unavailable_text("чат не найден в аккаунте")
    assert not is_unavailable_text("FloodWaitError: wait 30")
