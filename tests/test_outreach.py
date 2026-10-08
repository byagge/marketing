from pathlib import Path

import pytest
from test_pair_policy import FakeAPI, patches  # noqa: E402

from app.jobs import LogSink
from app.jobs import prefs as prefs_job
from app.jobs import setup as setup_job
from app.jobs import spam as spam_job
from app.jobs import stoplist as stoplist_job
from app.models import Account, Chat, Post, SpamState
from app.store import Store
from app.ui.autopilot_screens import autopilot_html, campaign_html
from app.utils.outreach import match_accounts, split_names
from app.utils.send_policy import REASON_LABEL, REASON_OUTREACH, is_send_allowed
from app.utils.spamstate import parse_spambot_reply

LIMITED = parse_spambot_reply("К сожалению, ваш аккаунт ограничен до 9 января 2027")
CLEAN = parse_spambot_reply("Good news, no limits are currently applied to your account.")


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "out.db")
    await db.init()
    return db


@pytest.fixture
def api(monkeypatch):
    FakeAPI.calls = []
    FakeAPI.live = []
    for mod in (setup_job, spam_job, stoplist_job):
        monkeypatch.setattr(mod, "SenderAPI", FakeAPI)
    monkeypatch.setattr(prefs_job, "_api", lambda: FakeAPI())
    return FakeAPI


async def _out(store, label="out1", **fields):
    acc = await store.add_account(label)
    await store.update_account(acc.id, outreach=1, telethon_session="/x", **fields)
    return await store.get_account(acc.id)


def test_policy_outreach_blocks_sender_only():
    acc = Account(id=1, label="o", outreach=1)
    sender = Chat(id=1, title="S", chat_id="-1", kind="sender")
    sched = Chat(id=2, title="C", chat_id="-2", kind="schedule")
    assert is_send_allowed(acc, sender, None, False) == (False, REASON_OUTREACH)
    assert is_send_allowed(acc, sched, None, False) == (True, "")
    assert "аутрич" in REASON_LABEL[REASON_OUTREACH]
    # аутрич сильнее dead по формулировке причины, schedule у dead тоже работает
    both = Account(id=1, label="o", outreach=1, dead=1)
    assert is_send_allowed(both, sender, None, False)[1] == REASON_OUTREACH
    assert is_send_allowed(both, sched, None, False) == (True, "")


async def test_outreach_load_is_zero_and_spamblocks_never_make_dead(store, monkeypatch):
    acc = await _out(store)
    assert await spam_job.account_load(store, acc) == 0

    async def fake_check(client, **kw):
        return box[0]

    monkeypatch.setattr(spam_job, "check_spambot", fake_check)
    box = [LIMITED]
    for i in range(6):  # шесть спамблоков подряд
        box[0] = LIMITED
        res = await spam_job.run_spam_check(store, await store.get_account(acc.id), object(), force=True)
        assert res.events == []  # без событий → без шума в сводках
        box[0] = CLEAN
        await spam_job.run_spam_check(store, await store.get_account(acc.id), object(), force=True)
    fresh = await store.get_account(acc.id)
    assert not fresh.is_dead and fresh.is_outreach
    st = await store.get_spam_state(acc.id)
    assert st.strikes == 6  # но статистика копится — видно в карточке


async def test_outreach_sender_is_stopped_and_schedule_still_plans(store, api, monkeypatch):
    acc = await _out(store, sender_account_id="sid1")
    a = await store.add_chat("Услуги", "-1001", kind="sender")
    sched = await store.add_chat("Sched", "-1003", kind="schedule")
    api.live = [{"chat_id": "-1001", "title": "Услуги"}]
    posts = {"ru": Post(lang="ru", text="hello"), "en": Post(lang="en"),
             "ru_short": Post(lang="ru_short"), "en_short": Post(lang="en_short")}
    job = await store.create_job("t", acc.id)
    log = LogSink(store, job.id)

    n = await setup_job._configure_sender(store, acc, posts, [sched], [a], log)
    assert n == 0 and ("spam_stop", "sid1") in api.calls
    assert not any(c[0] in {"put_post", "spam_start", "patch_chat", "put_interval"} for c in api.calls)

    # schedule для аутрича работает как обычно
    from types import SimpleNamespace

    from app.config import Settings

    plans = []

    async def fake_lookup(client, chat):
        return SimpleNamespace(title=chat.title)

    async def fake_photos(client, entity):
        return True

    async def fake_plan(**kw):
        plans.append(kw)
        return {"title": "T", "planned": 1, "success": 1, "error": "", "first_time": "09:00",
                "cleared": 0, "mode": "post", "used_photo": False, "media_blocked": False}

    monkeypatch.setattr(setup_job, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(setup_job, "lookup_entity", fake_lookup)
    monkeypatch.setattr(setup_job, "peer_allows_photos", fake_photos)
    monkeypatch.setattr(setup_job, "schedule_chat_posts", fake_plan)
    out = await setup_job.schedule_one_chat(store, object(), acc, sched, posts, log)
    assert out == "ok" and plans[0]["text"] == "hello"


async def test_marking_outreach_syncs_sender_off(store, api):
    acc = await store.add_account("tron")
    await store.update_account(acc.id, sender_account_id="sid1", outreach=1)
    msg = await prefs_job.apply_account_sender(store, acc.id)
    assert "аутрич" in msg and ("spam_stop", "sid1") in api.calls
    assert (await store.get_spam_state(acc.id)).applied_load == 0


async def test_apply_pair_for_outreach_sender_chat_deactivates(store, api):
    acc = await _out(store, sender_account_id="sid1")
    a = await store.add_chat("Услуги", "-1001", kind="sender")
    await store.save_post(acc.id, "ru", "t", [])
    api.live = [{"chat_id": "-1001"}]
    msg = await prefs_job.apply_pair(store, acc.id, a.id)
    assert "аутрич" in msg and {"active": False} in patches(api, "-1001")


def test_campaign_line_for_outreach():
    acc = Account(id=1, label="o", outreach=1)
    ok = campaign_html(acc, SpamState(account_id=1), 0, 4)
    assert "АУТРИЧ" in ok and "только schedule: 4 чатов" in ok and "спамблок" not in ok
    lim = SpamState(account_id=1, status="limited", limited_since="2026-10-08T10:00:00+00:00", strikes=7)
    text = campaign_html(acc, lim, 0, 4)
    assert "сейчас спамблок" in text and "нормально для аутрича" in text and "спамблоков было: 7" in text
    assert "DEAD" not in text


def test_autopilot_screen_shows_outreach_counts():
    assert "Аутрич-аккаунтов: <b>12</b>" in autopilot_html(True, "", "", 0, False, 12, 5)
    assert "Аутрич" not in autopilot_html(True, "", "", 0, False)


def test_name_matching():
    accs = [
        Account(id=1, label="Tron", username="tron_x", phone="+79990001122"),
        Account(id=2, label="alex"),
        Account(id=3, label="alex2"),
    ]
    assert split_names("a, b;c\n d ,, ") == ["a", "b", "c", "d"]
    found, missing = match_accounts("tron, @ALEX, #3, 79990001122, ghost, tron_x", accs)
    assert [a.id for a in found] == [1, 2, 3]  # без дублей
    assert missing == ["ghost"]
    assert match_accounts("", accs) == ([], [])


async def test_bulk_handler_marks_and_unmarks(store, monkeypatch):
    from types import SimpleNamespace

    from app.bot.handlers import prefs as ph
    from app.context import ctx

    ctx.store = store
    a = await store.add_account("tron")
    b = await store.add_account("alex")
    await store.add_account("keep")
    applied = []

    async def fake_apply(store_, account_id, bot, admin):
        applied.append(account_id)
        return "ok"

    sent = []

    async def finish(message, text, markup, photo=None):
        sent.append(text)

    monkeypatch.setattr(ph, "apply_account_sender", fake_apply)
    monkeypatch.setattr(ph, "finish_input", finish)

    class St:
        async def clear(self):
            return None

    class Bot:
        async def send_message(self, *a, **k):
            return None

    msg = SimpleNamespace(text="tron, alex, ghost", bot=Bot(), chat=SimpleNamespace(id=1))
    await ph.on_outreach_bulk(msg, St())
    import asyncio

    await asyncio.sleep(0.05)
    assert (await store.get_account(a.id)).is_outreach and (await store.get_account(b.id)).is_outreach
    accs = {x.label: x for x in await store.list_accounts()}
    assert not accs["keep"].is_outreach
    assert "Помечено аутрич: <b>2</b>" in sent[0] and "ghost" in sent[0]
    assert sorted(applied) == sorted([a.id, b.id])

    msg2 = SimpleNamespace(text="- tron", bot=Bot(), chat=SimpleNamespace(id=1))
    await ph.on_outreach_bulk(msg2, St())
    assert not (await store.get_account(a.id)).is_outreach
    assert (await store.get_account(b.id)).is_outreach
    assert "Снята пометка: <b>1</b>" in sent[1]


async def test_outreach_toggle_button(store, monkeypatch):
    from app.bot import keyboards as kb
    from app.bot.handlers import prefs as ph
    from app.bot.keyboards import MenuCB
    from app.context import ctx

    ctx.store = store
    acc = await store.add_account("tron")
    shown = []

    async def fake_edit(event, text, markup=None, **kw):
        shown.append((text, markup))

    async def fake_apply(*a, **k):
        return "ok"

    import app.bot.handlers.accounts as ah

    monkeypatch.setattr(ah, "safe_edit", fake_edit)
    monkeypatch.setattr(ph, "apply_account_sender", fake_apply)

    class Q:
        from_user = type("U", (), {"id": 1})()
        bot = type("B", (), {"send_message": staticmethod(lambda *a, **k: _coro())})()
        message = object()
        answers = []

        async def answer(self, text="", show_alert=False, **kw):
            self.answers.append(text)

    async def _coro():
        return None

    q = Q()
    await ph.cb_outreach_toggle(q, MenuCB(a="acc_outreach", i=acc.id))
    assert (await store.get_account(acc.id)).is_outreach
    assert any("АУТРИЧ" in t for t, _ in shown)
    labels = [b.text for row in shown[-1][1].inline_keyboard for b in row]
    assert "Аутрич: ВКЛ (убрать)" in labels
    labels_off = [b.text for row in kb.account_kb(await store.get_account(acc.id)).inline_keyboard for b in row]
    assert "Аутрич: ВКЛ (убрать)" in labels_off
    await ph.cb_outreach_toggle(q, MenuCB(a="acc_outreach", i=acc.id))
    assert not (await store.get_account(acc.id)).is_outreach


async def test_legacy_db_gets_outreach_column(tmp_path: Path):
    import aiosqlite

    path = tmp_path / "old.db"
    s = Store(path)
    await s.init()
    async with aiosqlite.connect(path) as db:
        await db.execute("ALTER TABLE accounts DROP COLUMN outreach")
        await db.commit()
    s2 = Store(path)
    await s2.init()
    acc = await s2.add_account("x")
    assert acc.outreach == 0
    await s2.update_account(acc.id, outreach=1)
    assert (await s2.get_account(acc.id)).is_outreach


def test_closed_vs_open_chat_detection():
    from app.tg.join import is_closed_chat

    def mk(**kw):
        base = dict(id=1, title="C", chat_id="-100", kind="schedule")
        base.update(kw)
        return Chat(**base)

    assert is_closed_chat(mk(invite_link="https://t.me/+AbC123"))
    assert is_closed_chat(mk(invite_link="https://t.me/joinchat/AbC123"))
    assert is_closed_chat(mk(invite_link="https://t.me/addlist/xyz"))
    assert is_closed_chat(mk(join_mode="garant", garant_bot="GuardBot"))
    assert not is_closed_chat(mk(username="open_chat"))
    assert not is_closed_chat(mk(invite_link="https://t.me/open_chat"))
    assert not is_closed_chat(mk())  # только id — считаем открытым
