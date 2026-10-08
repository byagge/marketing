from pathlib import Path

import pytest

from app.bot import kb_pairs
from app.bot.keyboards import account_kb, chat_kb, main_menu
from app.models import Account
from app.store import Store
from app.ui.autopilot_screens import autopilot_html, pair_card_html, pair_list_html


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "ui.db")
    await db.init()
    return db


def _all_callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def test_pair_list_search_and_paging_keyboard(store: Store):
    acc = await store.add_account("tron")
    chats = [await store.add_chat(f"Чат {i}", f"-100{i}") for i in range(11)]
    await store.set_chat_send_enabled(acc.id, chats[0].id, False)
    await store.set_chat_text(acc.id, chats[1].id, "свой")
    await store.record_ban(acc.id, chats[2].id, "removed")
    prefs = await store.prefs_for_account(acc.id)
    bans = {b.chat_pk: b for b in await store.list_bans()}

    kb = kb_pairs.pair_list_kb(acc.id, chats, prefs, bans, page=0, query="")
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any(t.startswith("⛔") for t in texts)
    assert any(t.startswith("🚫") for t in texts)
    assert any(t.startswith("✅") and t.endswith("✎") for t in texts)
    assert any("1/2" in t for t in texts)
    assert any("Выкл все (11)" in t for t in texts)
    assert all(len(c.encode()) <= 64 for c in _all_callbacks(kb))

    kb2 = kb_pairs.pair_list_kb(acc.id, chats[:3], prefs, bans, page=0, query="чат")
    texts2 = [b.text for row in kb2.inline_keyboard for b in row]
    assert "Сбросить поиск" in texts2
    assert any("найденные (3)" in t for t in texts2)


async def test_card_and_screens_render(store: Store):
    acc = await store.add_account("tron")
    chat = await store.add_chat("Øgma <x>", "-1001", kind="sender")
    await store.update_chat(chat.id, max_posts_per_account=3)
    chat = await store.get_chat(chat.id)
    await store.set_chat_text(acc.id, chat.id, "Строго <по> структуре")
    await store.record_ban(acc.id, chat.id, "send_ban", "UserBanned <x>")
    pref = await store.get_chat_pref(acc.id, chat.id)
    ban = await store.get_ban(acc.id, chat.id)

    html = pair_card_html(acc, chat, pref, ban, None)
    assert "&lt;x&gt;" in html and "&lt;по&gt;" in html and "3 постов" in html
    kb = kb_pairs.pair_card_kb(acc.id, chat.id, pref, ban)
    actions = " ".join(_all_callbacks(kb))
    assert "acp_tgl" in actions and "acp_txtclr" in actions and "acp_unban" in actions

    off_acc = Account(id=1, label="a", sender_enabled=0)
    assert "ВЫКЛЮЧЕН" in pair_list_html(off_acc, 1, 1, "q", 0, 0, 0)
    ap = autopilot_html(True, "2026-10-07T12:00:00+00:00", "всё <ок>", 2, False)
    assert "всё &lt;ок&gt;" in ap and "2026-10-07 12:00" in ap
    for kb in (kb_pairs.autopilot_kb(True, False), kb_pairs.bans_kb(), kb_pairs.diag_kb(5)):
        assert all(len(c.encode()) <= 64 for c in _all_callbacks(kb))


async def test_menus_expose_new_features(store: Store):
    acc = await store.add_account("tron")
    chat = await store.add_chat("C", "-1001")
    assert {"m:ap:0:0", "m:bans:0:0"} <= set(_all_callbacks(main_menu()))
    acts = " ".join(_all_callbacks(account_kb(acc)))
    assert "acp_list" in acts and "acc_sender" in acts
    acts = " ".join(_all_callbacks(chat_kb(chat)))
    assert "chat_lim" in acts and "chat_diag" in acts


async def test_main_menu_has_redesign_button_and_reports_section(store: Store, monkeypatch):
    assert "m:rd:0:0" in _all_callbacks(main_menu())

    from types import SimpleNamespace

    from app.bot.handlers import ops
    from app.context import ctx

    ctx.store = store
    shown = []

    async def fake_edit(event, text, markup=None, **kw):
        shown.append(text)

    monkeypatch.setattr(ops, "safe_edit", fake_edit)

    class Q:
        async def answer(self, *a, **k):
            return None

    await ops.cb_reports(Q())
    assert "Аккаунты для переоформления" in shown[0]
