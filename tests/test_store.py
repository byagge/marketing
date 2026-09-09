from pathlib import Path

import pytest

from app.store import Store
from app.utils.minutes import suggest_minute, period_for_interval


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "t.db")
    await db.init()
    return db


async def test_allocate_and_unique(store: Store):
    acc1 = await store.add_account("a1")
    acc2 = await store.add_account("a2")
    chat = await store.add_chat("Casino", "-1001", kind="schedule", interval_minutes=60)
    m1 = suggest_minute(period_for_interval(60), await store.occupied_minutes(chat.id))
    await store.set_slot(chat.id, acc1.id, m1)
    m2 = suggest_minute(period_for_interval(60), await store.occupied_minutes(chat.id))
    await store.set_slot(chat.id, acc2.id, m2)
    slots = await store.slots_for_chat(chat.id)
    assert [s.start_minute for s in slots] == [0, 30]

    try:
        await store.set_slot(chat.id, acc2.id, 0)
        raise AssertionError("same minute must fail")
    except ValueError:
        pass


async def test_posts_isolated_per_account(store: Store):
    a1 = await store.add_account("one")
    a2 = await store.add_account("two")
    await store.save_post(a1.id, "ru", "text-one", [{"type": "bold"}])
    await store.save_post(a2.id, "ru", "text-two", [])
    await store.save_post(a1.id, "en", "en-one", [])
    p1 = await store.get_post(a1.id, "ru")
    p2 = await store.get_post(a2.id, "ru")
    assert p1.text == "text-one"
    assert p2.text == "text-two"
    assert p1.account_id == a1.id
    assert p2.account_id == a2.id
    assert (await store.get_post(a2.id, "en")).text == ""
    assert (await store.get_post(a1.id, "en")).text == "en-one"
    await store.delete_account(a1.id)
    assert (await store.get_post(a1.id, "ru")).text == ""
    assert (await store.get_post(a2.id, "ru")).text == "text-two"


async def test_legacy_posts_copied_per_account(tmp_path: Path):
    import aiosqlite

    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                user_id INTEGER,
                telethon_session TEXT NOT NULL DEFAULT '',
                pyrogram_session TEXT NOT NULL DEFAULT '',
                sender_bot_token TEXT NOT NULL DEFAULT '',
                sender_account_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'idle',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE posts (
                lang TEXT PRIMARY KEY,
                text TEXT NOT NULL DEFAULT '',
                entities_json TEXT NOT NULL DEFAULT '[]',
                photo_path TEXT NOT NULL DEFAULT ''
            );
            """
        )
        await db.execute("INSERT INTO accounts(label) VALUES('old')")
        await db.execute(
            "INSERT INTO posts(lang, text, entities_json, photo_path) VALUES('ru', 'shared-text', '[]', '')"
        )
        await db.commit()
    db = Store(path)
    await db.init()
    accs = await db.list_accounts()
    assert len(accs) == 1
    assert (await db.get_post(accs[0].id, "ru")).text == "shared-text"
    a2 = await db.add_account("new")
    assert (await db.get_post(a2.id, "ru")).text == ""
