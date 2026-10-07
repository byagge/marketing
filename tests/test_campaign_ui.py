import asyncio
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from app.bot import keyboards as kb
from app.bot.handlers import accounts as acc_handlers
from app.bot.handlers import autopilot as ap_handlers
from app.bot.handlers import prefs as pref_handlers
from app.bot.keyboards import MenuCB
from app.context import ctx
from app.jobs import runtime
from app.models import Account, SpamState
from app.store import Store
from app.tg.spambot import check_spambot
from app.ui.autopilot_screens import campaign_html, stoplist_html
from app.ui.screens import account_html


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "ui2.db")
    await db.init()
    ctx.store = db
    return db


def acc(**kw) -> Account:
    base = dict(id=1, label="tron", sender_account_id="sid1", telethon_session="/x")
    base.update(kw)
    return Account(**base)


def test_campaign_variants():
    st = SpamState(account_id=1)
    assert "в кампании" in campaign_html(acc(), st, 100, 7)
    assert "schedule: 7 чатов" in campaign_html(acc(), st, 100, 7)
    assert "DEAD" in campaign_html(acc(dead=1), st, 0, 3) and "только schedule: 3 чатов" in campaign_html(acc(dead=1), st, 0, 3)
    off = campaign_html(acc(sender_enabled=0), st, 100, 2)
    assert "отключён вручную" in off and "в кампании только" in off
    lim = SpamState(account_id=1, status="limited", limited_since="2026-10-08T10:00:00+00:00",
                    limited_until="2026-10-09T10:00:00+00:00", strikes=2)
    text = campaign_html(acc(), lim, 25, 4, dead_strikes=3)
    assert "спамблок" in text and "sender 25%" in text and "2/3" in text and "без изменений" in text
    assert "sender остановлен" in campaign_html(acc(), lim, 0, 4)
    rec = SpamState(account_id=1, cleared_at="2026-10-08T10:00:00+00:00", strikes=1)
    assert "восстановление" in campaign_html(acc(), rec, 50, 4)
    assert "sender не настроен" in campaign_html(Account(id=2, label="x"), st, 100, 0)
    assert "нет настроенных чатов" in campaign_html(acc(), st, 100, 0)


def test_account_card_shows_campaign_line():
    text = account_html(acc(), campaign="КАМПАНИЯ-СТРОКА")
    assert "КАМПАНИЯ-СТРОКА" in text
    assert "КАМПАНИЯ" not in account_html(acc())


def labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_account_keyboard_buttons():
    on = labels(kb.account_kb(acc()))
    assert "Отключить отправку по sender" in on
    assert "Проверить спамблок" in on and "Перевести в dead (только schedule)" in on
    off = labels(kb.account_kb(acc(sender_enabled=0, dead=1)))
    assert "Включить отправку по sender" in off and "Вернуть из dead" in off


async def test_chat_card_has_no_post_toggle(store):
    chat = await store.add_chat("Отзывы", "-1001")
    assert "Писать нельзя: нет" in labels(kb.chat_kb(chat))
    await store.update_chat(chat.id, no_post=1)
    assert "Писать нельзя: ДА" in labels(kb.chat_kb(await store.get_chat(chat.id)))


def test_stoplist_screen_escapes():
    html = stoplist_html(("отзыв", "<b>"), ["Отзывы <x>"])
    assert "&lt;b&gt;" in html and "Отзывы &lt;x&gt;" in html


# ---- хендлеры ---------------------------------------------------------------


class FakeQuery:
    def __init__(self):
        self.answers = []
        self.from_user = SimpleNamespace(id=42)
        self.bot = FakeBot()
        self.message = SimpleNamespace()

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def _drain():
    for _ in range(5):
        tasks = [t for t in runtime._tasks.values() if t and not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


@pytest.fixture
def screens(monkeypatch):
    shown = []

    async def fake_edit(event, text, markup=None, **kw):
        shown.append((text, markup))

    for mod in (pref_handlers, ap_handlers, acc_handlers):
        monkeypatch.setattr(mod, "safe_edit", fake_edit, raising=False)
    return shown


async def test_dead_toggle_flow(store, screens, monkeypatch):
    a = await store.add_account("tron")
    applied = []

    async def fake_apply(store_, account_id, bot, admin):
        applied.append(account_id)
        return "готово"

    monkeypatch.setattr(pref_handlers, "apply_account_sender", fake_apply)
    q = FakeQuery()

    await pref_handlers.cb_dead_toggle(q, MenuCB(a="acc_dead", i=a.id))
    await _drain()
    assert (await store.get_account(a.id)).is_dead
    assert applied == [a.id] and "dead" in q.answers[-1][0]
    assert q.bot.sent and "готово" in q.bot.sent[0][1]
    assert any("DEAD" in t for t, _ in screens)  # карточка перерисована со статусом

    st = await store.get_spam_state(a.id)
    st.strikes = 3
    await store.save_spam_state(st)
    await pref_handlers.cb_dead_toggle(q, MenuCB(a="acc_dead", i=a.id))
    await _drain()
    assert not (await store.get_account(a.id)).is_dead
    assert (await store.get_spam_state(a.id)).strikes == 0  # иначе следующий же спамблок вернёт в dead


async def test_sender_toggle_flow_updates_card(store, screens, monkeypatch):
    a = await store.add_account("tron")
    calls = []

    async def fake_apply(store_, account_id, bot, admin):
        calls.append(account_id)
        return "ок"

    monkeypatch.setattr(pref_handlers, "apply_account_sender", fake_apply)
    q = FakeQuery()
    await pref_handlers.cb_sender_toggle(q, MenuCB(a="acc_sender", i=a.id))
    await _drain()
    assert not (await store.get_account(a.id)).sender_on and calls == [a.id]
    assert any("sender отключён вручную" in t for t, _ in screens)
    off_markup = screens[-1][1]
    assert "Включить отправку по sender" in labels(off_markup)


async def test_no_post_toggle_flow(store, screens, monkeypatch):
    chat = await store.add_chat("Отзывы", "-1001")
    applied = []

    async def fake_all(store_, chat_pk, bot, admin):
        applied.append(chat_pk)
        return "применено"

    monkeypatch.setattr("app.jobs.prefs.apply_chat_all", fake_all)
    q = FakeQuery()
    await ap_handlers.cb_nopost(q, MenuCB(a="chat_nopost", i=chat.id))
    await _drain()
    assert (await store.get_chat(chat.id)).no_post == 1 and applied == [chat.id]
    assert "Писать нельзя" in q.answers[-1][0]
    await ap_handlers.cb_nopost(q, MenuCB(a="chat_nopost", i=chat.id))
    await _drain()
    assert (await store.get_chat(chat.id)).no_post == 0


async def test_stoplist_screens_and_edit(store, screens, monkeypatch):
    await store.add_chat("Отзывы клиентов", "-1001")
    await store.add_chat("Услуги", "-1002")
    state = SimpleNamespace(clear=_noop)
    await ap_handlers.cb_stoplist(FakeQuery(), state)
    text, _ = screens[-1]
    assert "Отзывы клиентов" in text and "Услуги" not in text.split("Сейчас под запретом")[1]

    sent = []

    async def finish(message, text, markup, photo=None):
        sent.append(text)

    monkeypatch.setattr(ap_handlers, "finish_input", finish)
    msg = SimpleNamespace(text="Фидбэк, Отзыв ,review")
    await ap_handlers.on_stoplist_words(msg, state)
    assert await store.get_stoplist() == ("review", "отзыв", "фидбэк")
    assert sent and "Применить сейчас" in sent[0]


async def _noop(*a, **k):
    return None


async def test_spam_check_button_flow(store, monkeypatch):
    a = await store.add_account("tron")
    await store.update_account(a.id, telethon_session="/x", sender_account_id="sid")
    from contextlib import asynccontextmanager

    from app.jobs import spam as spam_job

    @asynccontextmanager
    async def fake_client(path):
        yield object()

    monkeypatch.setattr("app.tg.client.telethon_client", fake_client)

    async def fake_check(client, **kw):
        from app.utils.spamstate import parse_spambot_reply

        return parse_spambot_reply("К сожалению, ваш аккаунт ограничен до 9 января 2027")

    synced = []

    async def fake_sync(store_, account_id, bot=None, admin=None):
        synced.append(account_id)
        return "нагрузка sender 50%"

    monkeypatch.setattr(spam_job, "check_spambot", fake_check)
    monkeypatch.setattr(spam_job, "sync_sender_load", fake_sync)
    q = FakeQuery()
    await pref_handlers.cb_spam_check(q, MenuCB(a="acc_spam", i=a.id))
    await _drain()
    st = await store.get_spam_state(a.id)
    assert st.is_limited and st.strikes == 1
    assert synced == [a.id]
    assert "спамблок" in q.bot.sent[0][1] and "нагрузка sender 50%" in q.bot.sent[0][1]


async def test_spam_check_requires_telethon(store):
    a = await store.add_account("nosession")
    q = FakeQuery()
    await pref_handlers.cb_spam_check(q, MenuCB(a="acc_spam", i=a.id))
    assert q.answers[-1] == ("Нужен Telethon session", True)


# ---- check_spambot с поддельным клиентом -----------------------------------


class SpamBotClient:
    def __init__(self, reply):
        self.reply = reply
        self.sent = []
        self.started = False

    async def get_entity(self, name):
        return SimpleNamespace(id=1)

    async def get_messages(self, bot, limit=1):
        return [SimpleNamespace(id=5)]

    async def send_message(self, bot, text):
        self.sent.append(text)
        self.started = True

    async def iter_messages(self, bot, limit=5):
        if not self.started:
            yield SimpleNamespace(id=5, out=False, message="старый ответ: ограничен")
            return
        yield SimpleNamespace(id=7, out=False, message=self.reply)
        yield SimpleNamespace(id=6, out=True, message="/start")
        yield SimpleNamespace(id=5, out=False, message="старый ответ: ограничен")


async def test_check_spambot_reads_only_the_new_reply():
    st = await check_spambot(SpamBotClient("Ваш аккаунт свободен от каких-либо ограничений."), poll_sec=0.01)
    assert st.known and not st.limited  # старое сообщение не учитывается
    st = await check_spambot(SpamBotClient("Unfortunately, your account was limited"), poll_sec=0.01)
    assert st.known and st.limited


async def test_check_spambot_timeout_is_unknown():
    st = await check_spambot(SpamBotClient("???"), wait_sec=0.05, poll_sec=0.01)
    assert not st.known


# ---- миграция живой БД из прошлой версии -----------------------------------


async def test_migration_from_previous_release_db(tmp_path: Path):
    """БД уже с join_states/prefs предыдущего релиза (без gate_*, dead, no_post)."""
    path = tmp_path / "prev.db"
    store = Store(path)
    await store.init()
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            DROP TABLE join_states;
            CREATE TABLE join_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
                chat_pk INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                fail_count INTEGER NOT NULL DEFAULT 0, miss_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT NOT NULL DEFAULT '', last_member_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '', UNIQUE(account_id, chat_pk));
            INSERT INTO join_states(account_id, chat_pk, status) VALUES (1, 1, 'member');
            DROP TABLE spam_states;
            """
        )
        await db.commit()
    fresh = Store(path)
    await fresh.init()
    state = await fresh.get_join_state(1, 1)
    assert state.status == "member" and state.gate_count == 0 and state.gate_at == ""
    await fresh.mark_gate(1, 1, msg_id=9, note="ok", resolved=True)
    assert (await fresh.get_join_state(1, 1)).gate_count == 1
    assert (await fresh.get_spam_state(1)).status == "clean"
