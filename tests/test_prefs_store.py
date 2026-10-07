from pathlib import Path

import aiosqlite
import pytest

from app.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "prefs.db")
    await db.init()
    return db


async def test_new_columns_have_safe_defaults(store: Store):
    acc = await store.add_account("a")
    chat = await store.add_chat("C", "-1001")
    assert acc.sender_on is True
    assert chat.max_posts_per_account == 0
    await store.update_account(acc.id, sender_enabled=0)
    assert (await store.get_account(acc.id)).sender_on is False
    await store.update_chat(chat.id, max_posts_per_account=3)
    assert (await store.get_chat(chat.id)).max_posts_per_account == 3


async def test_pair_pref_defaults_and_toggle(store: Store):
    acc = await store.add_account("a")
    chat = await store.add_chat("C", "-1001")
    pref = await store.get_chat_pref(acc.id, chat.id)
    assert pref.enabled and not pref.has_text

    pref = await store.set_chat_send_enabled(acc.id, chat.id, False)
    assert not pref.enabled
    pref = await store.set_chat_send_enabled(acc.id, chat.id, True)
    assert pref.enabled


async def test_pair_text_roundtrip_keeps_toggle(store: Store):
    acc = await store.add_account("a")
    chat = await store.add_chat("Øgma", "-1002")
    await store.set_chat_send_enabled(acc.id, chat.id, False)
    ents = [{"type": "bold", "offset": 0, "length": 3}]
    pref = await store.set_chat_text(acc.id, chat.id, "Строго\nпо структуре", ents)
    assert pref.has_text and pref.text == "Строго\nпо структуре"
    assert not pref.enabled  # выключатель не затёрт сохранением текста
    assert '"bold"' in pref.entities_json

    pref = await store.clear_chat_text(acc.id, chat.id)
    assert not pref.has_text
    assert not pref.enabled


async def test_text_is_per_account_per_chat(store: Store):
    a1 = await store.add_account("a1")
    a2 = await store.add_account("a2")
    c1 = await store.add_chat("C1", "-1001")
    c2 = await store.add_chat("C2", "-1002")
    await store.set_chat_text(a1.id, c1.id, "t11")
    await store.set_chat_text(a2.id, c1.id, "t21")
    assert (await store.get_chat_pref(a1.id, c1.id)).text == "t11"
    assert (await store.get_chat_pref(a2.id, c1.id)).text == "t21"
    assert not (await store.get_chat_pref(a1.id, c2.id)).has_text
    assert set((await store.prefs_for_chat(c1.id)).keys()) == {a1.id, a2.id}
    assert set((await store.prefs_for_account(a1.id)).keys()) == {c1.id}


async def test_bulk_toggle(store: Store):
    acc = await store.add_account("a")
    chats = [await store.add_chat(f"C{i}", f"-100{i}") for i in range(3)]
    n = await store.set_chat_send_enabled_bulk(acc.id, [c.id for c in chats[:2]], False)
    assert n == 2
    prefs = await store.prefs_for_account(acc.id)
    assert [p.enabled for p in prefs.values()] == [False, False]
    assert chats[2].id not in prefs


async def test_ban_lifecycle(store: Store):
    acc = await store.add_account("tron")
    chat = await store.add_chat("MARKET 404", "-1001")

    ban, is_new = await store.record_ban(acc.id, chat.id, "join_ban", "banned")
    assert is_new and ban.active and ban.account_label == "tron" and ban.chat_title == "MARKET 404"
    assert (await store.active_ban_pairs()) == {(acc.id, chat.id)}

    ban2, is_new2 = await store.record_ban(acc.id, chat.id, "join_ban", "banned again")
    assert not is_new2 and ban2.id == ban.id

    await store.mark_ban_notified([ban.id])
    assert (await store.get_ban(acc.id, chat.id)).notified == 1

    assert await store.clear_ban(acc.id, chat.id)
    assert await store.active_ban_pairs() == set()
    assert (await store.list_bans(active_only=False))[0].active == 0

    # повторный бан после снятия — снова «новый» и снова не уведомлён
    ban3, is_new3 = await store.record_ban(acc.id, chat.id, "removed")
    assert is_new3 and ban3.notified == 0 and ban3.reason == "removed"


async def test_mark_member_clears_only_join_and_removed_bans(store: Store):
    acc = await store.add_account("a")
    c1 = await store.add_chat("C1", "-1001")
    c2 = await store.add_chat("C2", "-1002")
    await store.record_ban(acc.id, c1.id, "removed")
    await store.record_ban(acc.id, c2.id, "send_ban")
    await store.mark_member(acc.id, c1.id)
    await store.mark_member(acc.id, c2.id)
    assert await store.active_ban_pairs() == {(acc.id, c2.id)}  # бан на отправку остаётся


async def test_join_state_misses_and_reset(store: Store):
    acc = await store.add_account("a")
    chat = await store.add_chat("C", "-1001")
    st = await store.mark_not_member(acc.id, chat.id)
    assert st.miss_count == 1 and st.status == "pending" and not st.last_member_at

    await store.mark_member(acc.id, chat.id)
    st = await store.get_join_state(acc.id, chat.id)
    assert st.status == "member" and st.miss_count == 0 and st.last_member_at

    st = await store.mark_not_member(acc.id, chat.id)
    assert st.status == "pending" and st.miss_count == 1 and st.last_member_at
    st = await store.mark_not_member(acc.id, chat.id)
    assert st.miss_count == 2

    await store.reset_pair_join(acc.id, chat.id)
    st = await store.get_join_state(acc.id, chat.id)
    assert st.miss_count == 0 and st.last_member_at == "" and st.status == "pending"


async def test_join_fail_abandons_after_max(store: Store):
    acc = await store.add_account("a")
    chat = await store.add_chat("C", "-1001")
    for i in range(1, 4):
        st = await store.record_join_result(
            acc.id, chat.id, status="pending", error="boom", fail=True, max_attempts=3
        )
        assert st.fail_count == i
    assert st.status == "abandoned"
    assert await store.reset_join_states() == 1
    st = await store.get_join_state(acc.id, chat.id)
    assert st.status == "pending" and st.fail_count == 0


async def test_deleting_account_or_chat_cleans_new_tables(store: Store):
    acc = await store.add_account("a")
    chat = await store.add_chat("C", "-1001")
    other = await store.add_chat("D", "-1002")
    await store.set_chat_text(acc.id, chat.id, "x")
    await store.record_ban(acc.id, chat.id, "removed")
    await store.mark_member(acc.id, chat.id)
    await store.set_chat_text(acc.id, other.id, "y")

    await store.delete_chat(chat.id)
    assert (await store.prefs_for_account(acc.id)).keys() == {other.id}
    assert await store.list_bans(active_only=False) == []
    assert await store.get_join_state(acc.id, chat.id) is None

    await store.delete_account(acc.id)
    async with aiosqlite.connect(store.path) as db:
        for table in ("pair_prefs", "chat_bans", "join_states"):
            cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
            assert (await cur.fetchone())[0] == 0


async def test_migration_adds_columns_to_legacy_db(tmp_path: Path):
    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
                user_id INTEGER, telethon_session TEXT NOT NULL DEFAULT '',
                pyrogram_session TEXT NOT NULL DEFAULT '', sender_bot_token TEXT NOT NULL DEFAULT '',
                sender_account_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idle',
                last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO accounts(label, created_at, updated_at) VALUES('old','x','x');
            CREATE TABLE chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, chat_id TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'sender',
                lang TEXT NOT NULL DEFAULT 'ru', tag TEXT NOT NULL DEFAULT '',
                interval_minutes INTEGER NOT NULL DEFAULT 60, enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            INSERT INTO chats(title, chat_id, created_at) VALUES('old','-1','x');
            """
        )
        await db.commit()
    store = Store(path)
    await store.init()
    acc = (await store.list_accounts())[0]
    chat = (await store.list_chats())[0]
    assert acc.sender_on is True
    assert chat.max_posts_per_account == 0
