"""Муты и баны: журнал, разбор причины, уведомление и экраны панели."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.context import ctx
from app.jobs import restr_events as job
from app.store import Store
from app.tg.mutewhy import (
    Gathered,
    Ident,
    Msg,
    build_why,
    classify_message,
    describe_sends,
)
from app.ui.restr_view import ban_entries, entry_html, mute_entries

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "mw.db")
    await db.init()
    ctx.store = db
    return db


async def _pair(store: Store, label="acc", title="Чат услуг"):
    acc = await store.add_account(label)
    await store.update_account(acc.id, telethon_session="/tmp/x.session")
    chat = await store.add_chat(title, "-1001995593406", kind="schedule")
    return await store.get_account(acc.id), chat


# ---- классификация ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,cause",
    [
        ("Ваши сообщения похожи на автоматическую рассылку, отправка ограничена", "antispam"),
        ("Не повторяйте одно и то же сообщение", "duplicate"),
        ("Слишком часто, не чаще 1 раза в час", "frequency"),
        ("Ссылки запрещены", "links"),
        ("Реклама только в теме Услуги", "ads"),
        ("Пройдите капчу", "verify"),
        ("Нарушение правил чата", "rules"),
        ("Администратор выдал вам мут", "admin"),
    ],
)
def test_classify_message(text, cause):
    got = classify_message(text)
    assert got is not None and got[0] == cause


def test_classify_ignores_plain_chat():
    assert classify_message("всем привет, как дела?") is None
    assert classify_message("") is None


def test_ident_matches_username_and_name():
    ident = Ident(user_id=1, username="Alex_x", names=("Иван Петров",))
    assert ident.mentioned_in("@alex_x вы замьючены")
    assert ident.mentioned_in("Иван Петров, правила нарушены")
    assert not ident.mentioned_in("привет всем")


# ---- build_why ----------------------------------------------------------------------------


def _msg(text, *, minutes_ago=30, to_me=True, bot=True, sender="@AntiSpamBot"):
    return Msg(
        id=5,
        text=text,
        date=NOW - timedelta(minutes=minutes_ago),
        sender=sender,
        is_bot=bot,
        to_me=to_me,
        link="https://t.me/c/1995593406/5",
    )


def test_why_direct_message_from_bot():
    g = Gathered(
        messages=[_msg("@acc ваши сообщения похожи на автоматическую рассылку, мут на 24 часа")]
    )
    why = build_why("mute", gathered=g, detected=NOW)
    assert why.cause == "antispam" and why.certain
    assert "рассылк" in why.summary and "AntiSpamBot" in why.summary
    assert why.link.endswith("/5")
    assert why.evidence and "адресовано аккаунту" in why.evidence[0]


def test_why_bot_message_without_mention_is_probable():
    g = Gathered(messages=[_msg("Пользователь замьючен за спам", to_me=False)])
    why = build_why("mute", gathered=g, detected=NOW)
    assert why.cause == "antispam" and not why.certain
    assert why.summary.startswith("Похоже")


def test_why_generic_bot_header_is_not_a_reason():
    header = "Rules - https://telegra.ph/Rules-GSC Protection @WsGuardBot, запрещена реклама"
    g = Gathered(messages=[_msg(header, to_me=False)])
    # бот-шапка про запреты вообще, но не про наказание → не объяснение мута
    assert build_why("mute", gathered=g, detected=NOW).cause == "not_found"


def test_why_human_chat_talk_is_not_a_reason():
    g = Gathered(messages=[_msg("реклама достала", to_me=False, bot=False, sender="vasya")])
    why = build_why("mute", gathered=g, detected=NOW)
    assert why.cause == "not_found"


def test_why_old_messages_outside_window_ignored():
    g = Gathered(messages=[_msg("@acc автоматическая рассылка", minutes_ago=60 * 24 * 10)])
    assert build_why("mute", gathered=g, detected=NOW).cause == "not_found"


def test_why_addressed_but_unclassified_is_notice():
    g = Gathered(messages=[_msg("@acc здравствуйте, ответьте мне в личку")])
    why = build_why("mute", gathered=g, detected=NOW)
    assert why.cause == "notice"


def test_why_falls_back_to_chat_rules():
    g = Gathered(rules=[("ads", "Реклама только в закреплённой теме", "закреп")])
    why = build_why("mute", gathered=g, detected=NOW)
    assert why.cause == "ads" and "правилах чата" in why.summary


def test_why_pattern_after_our_send():
    sends = [NOW - timedelta(minutes=8), NOW - timedelta(hours=3)]
    why = build_why("mute", gathered=Gathered(), sends=sends, detected=NOW)
    assert why.cause == "pattern" and "предположение" in why.summary
    assert any("отправил в чат 2 раз" in e for e in why.evidence)


def test_why_unreadable_ban_and_not_found():
    bad = Gathered(readable=False, read_error="чат недоступен")
    why = build_why("ban", gathered=bad, detected=NOW)
    assert why.cause == "unreadable" and "бан" in why.summary
    assert build_why("ban", gathered=Gathered(), detected=NOW).cause == "not_found"


def test_describe_sends():
    assert "ничего не отправлял" in describe_sends([], NOW)
    assert "последняя отправка за 8 мин" in describe_sends([NOW - timedelta(minutes=8)], NOW)


# ---- журнал в Store -----------------------------------------------------------------------


async def test_new_mute_creates_one_event(store: Store):
    acc, chat = await _pair(store)
    await store.upsert_restriction(acc.id, chat.id, "mute", until_at="2026-10-11T12:00:00+00:00")
    await store.upsert_restriction(acc.id, chat.id, "mute", until_at="2026-10-11T12:00:00+00:00")
    events = await store.list_restr_events()
    assert len(events) == 1 and events[0].kind == "mute"
    assert events[0].analyzed == 0 and events[0].notified == 0
    assert events[0].account_label == "acc" and events[0].chat_title == "Чат услуг"


async def test_spamblock_and_nowrite_are_preset_and_silent(store: Store):
    acc, chat = await _pair(store)
    other = await store.add_chat("Канал", "-1001000000009", kind="schedule")
    await store.upsert_restriction(acc.id, chat.id, "spamblock")
    await store.upsert_restriction(acc.id, other.id, "nowrite")
    events = {e.kind: e for e in await store.list_restr_events()}
    assert events["spamblock"].cause == "account_spam" and events["spamblock"].notified == 1
    assert events["nowrite"].cause == "chat_closed" and events["nowrite"].analyzed == 1
    assert await store.pending_restr_events() == []
    assert await store.unnotified_restr_events() == []


async def test_ban_in_both_tables_is_one_event(store: Store):
    acc, chat = await _pair(store)
    await store.upsert_restriction(acc.id, chat.id, "ban")
    await store.record_ban(acc.id, chat.id, "removed")
    events = await store.list_restr_events()
    assert len(events) == 1 and events[0].kind == "ban"


async def test_adopt_existing_restrictions_silently(store: Store):
    acc, chat = await _pair(store)
    await store.upsert_restriction(acc.id, chat.id, "mute")
    async with store._connect() as db:  # имитация старой БД: записей журнала не было
        await db.execute("DELETE FROM restr_events")
        await db.commit()
    assert await store.adopt_restr_events() == 1
    ev = (await store.list_restr_events())[0]
    assert ev.notified == 1 and ev.analyzed == 0
    assert await store.adopt_restr_events() == 0


# ---- разбор + уведомление -----------------------------------------------------------------


class _Client:
    async def get_me(self):
        class Me:
            id = 77
            username = "acc_user"
            first_name = "Иван"
            last_name = "Петров"

        return Me()


def _patch_telegram(monkeypatch, gathered: Gathered):
    @asynccontextmanager
    async def fake_client(_path):
        yield _Client()

    async def fake_lookup(_client, _chat):
        return object()

    async def fake_gather(_client, _entity, ident, **_kw):
        assert ident.username == "acc_user" and ident.user_id == 77
        return gathered

    monkeypatch.setattr(job, "telethon_client", fake_client)
    monkeypatch.setattr(job, "lookup_entity", fake_lookup)
    monkeypatch.setattr(job, "gather", fake_gather)


class _Bot:
    def __init__(self):
        self.sent: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


async def test_analyze_and_notify_once(store: Store, monkeypatch):
    acc, chat = await _pair(store)
    await store.upsert_restriction(acc.id, chat.id, "mute", until_at="2999-01-02T00:00:00+00:00")
    ev = (await store.list_restr_events())[0]
    detected = datetime.fromisoformat(ev.detected_at)
    msg = Msg(
        id=9,
        text="@acc_user ваши сообщения похожи на автоматическую рассылку",
        date=detected - timedelta(minutes=2),
        sender="@WsGuardBot",
        is_bot=True,
        to_me=True,
        link="https://t.me/c/1995593406/9",
    )
    _patch_telegram(monkeypatch, Gathered(messages=[msg]))
    bot = _Bot()

    summary = await job.run_restr_events(store, bot, 42)
    assert "причин разобрано 1" in summary and "уведомлений 1" in summary
    assert len(bot.sent) == 1
    chat_id, text, kw = bot.sent[0]
    assert chat_id == 42 and kw["parse_mode"] == "HTML"
    assert "Мут" in text and "acc" in text and "Чат услуг" in text
    assert "автоматическ" in text and "WsGuardBot" in text and "Что делать" in text
    assert "t.me/c/1995593406/9" in text
    saved = (await store.list_restr_events())[0]
    assert saved.cause == "antispam" and saved.analyzed == 1 and saved.notified == 1

    again = await job.run_restr_events(store, bot, 42)
    assert "уведомлений 0" in again and len(bot.sent) == 1


async def test_many_new_events_go_as_one_digest(store: Store, monkeypatch):
    acc, _ = await _pair(store)
    for i in range(job.DETAILED_NOTICES + 2):
        c = await store.add_chat(f"Чат {i}", f"-100200000000{i}", kind="schedule")
        await store.upsert_restriction(acc.id, c.id, "mute")
    _patch_telegram(monkeypatch, Gathered())
    bot = _Bot()
    await job.run_restr_events(store, bot, 42, limit=50)
    assert len(bot.sent) == 1 and "Новые муты / баны: 8" in bot.sent[0][1]


async def test_analysis_failure_is_retried_then_gives_up(store: Store, monkeypatch):
    acc, chat = await _pair(store)
    await store.upsert_restriction(acc.id, chat.id, "ban")

    @asynccontextmanager
    async def broken(_path):
        raise RuntimeError("session revoked")
        yield  # pragma: no cover

    monkeypatch.setattr(job, "telethon_client", broken)
    for _ in range(job.MAX_ATTEMPTS):
        await job.analyze_pending(store)
    ev = (await store.list_restr_events())[0]
    assert ev.analyzed == 1 and ev.cause == "unreadable" and "session revoked" in ev.summary
    assert await store.pending_restr_events() == []


async def test_ban_falls_back_to_helper_account(store: Store, monkeypatch):
    acc, chat = await _pair(store)
    helper = await store.add_account("helper")
    await store.update_account(helper.id, telethon_session="/tmp/h.session")
    await store.upsert_restriction(acc.id, chat.id, "ban")

    calls: list[bool] = []

    @asynccontextmanager
    async def fake_client(_path):
        yield _Client()

    async def fake_lookup(_c, _chat):
        return object()

    async def fake_gather(_c, _e, ident, *, self_reader, **_kw):
        calls.append(self_reader)
        if self_reader:
            return Gathered(readable=False, read_error="чат недоступен")
        return Gathered(
            messages=[
                Msg(id=1, text="@acc_user забанен за спам", date=datetime.now(timezone.utc),
                    sender="@bot", is_bot=True, to_me=True)
            ]
        )

    monkeypatch.setattr(job, "telethon_client", fake_client)
    monkeypatch.setattr(job, "lookup_entity", fake_lookup)
    monkeypatch.setattr(job, "gather", fake_gather)
    await job.analyze_pending(store)
    ev = (await store.list_restr_events())[0]
    assert calls == [True, False]
    assert ev.cause == "antispam" and "спам" in ev.summary


# ---- экраны панели ------------------------------------------------------------------------


async def test_panel_lists_show_reason_when_where_and_both_ban_tables(store: Store):
    acc, chat = await _pair(store)
    other = await store.add_chat("Второй", "-1001000000002", kind="schedule")
    third = await store.add_chat("Третий", "-1001000000003", kind="schedule")
    await store.upsert_restriction(acc.id, chat.id, "mute", until_at="2999-01-02T00:00:00+00:00")
    await store.upsert_restriction(acc.id, other.id, "ban")
    await store.record_ban(acc.id, third.id, "removed", "kicked")
    ev = next(e for e in await store.list_restr_events() if e.kind == "mute")
    await store.save_restr_analysis(
        ev.id, cause="antispam", summary="антиспам: авторассылка", link="https://t.me/c/1/2"
    )

    mutes = await mute_entries(store)
    assert len(mutes) == 1
    html = entry_html(1, mutes[0])
    assert "acc" in html and "Чат услуг" in html and "антиспам: авторассылка" in html
    assert "2999" in html and "t.me/c/1/2" in html

    bans = await ban_entries(store)
    assert {b.chat_title for b in bans} == {"Второй", "Третий"}
    third_html = entry_html(2, next(b for b in bans if b.chat_title == "Третий"))
    assert "вылетел" in third_html and "kicked" in third_html


async def test_unexplained_entry_hints_next_step(store: Store):
    acc, chat = await _pair(store)
    await store.upsert_restriction(acc.id, chat.id, "mute")
    async with store._connect() as db:
        await db.execute("DELETE FROM restr_events")
        await db.commit()
    html = entry_html(1, (await mute_entries(store))[0])
    assert "Разобрать причины" in html


# ---- сбор из Telegram (поддельный клиент) -------------------------------------------------


class _Sender:
    def __init__(self, uid, username="", bot=False, first=""):
        self.id, self.username, self.bot, self.first_name = uid, username, bot, first


class _FakeMsg:
    def __init__(self, mid, text, sender, *, out=False, reply_to=None, date=None):
        self.id, self.message, self._sender, self.out = mid, text, sender, out
        self.entities = []
        self.date = date or datetime.now(timezone.utc)
        self.reply_to = type("R", (), {"reply_to_msg_id": reply_to})() if reply_to else None

    async def get_sender(self):
        return self._sender


class _FakeTg:
    def __init__(self, messages, mentions=()):
        self.messages, self.mentions = messages, set(mentions)

    async def iter_messages(self, entity, limit=None, filter=None, from_user=None):
        name = type(filter).__name__ if filter is not None else ""
        for m in self.messages:
            if name == "InputMessagesFilterMyMentions" and m.id not in self.mentions:
                continue
            if name == "InputMessagesFilterPinned":
                continue
            if from_user == "me" and not m.out:
                continue
            yield m

    async def get_messages(self, entity, ids=None):
        return next((m for m in self.messages if m.id == ids), None)


async def test_gather_finds_bot_notice_and_skips_noise():
    from app.tg.mutewhy import gather

    bot = _Sender(900, "WsGuardBot", bot=True)
    human = _Sender(5, "vasya", first="Вася")
    me = _Sender(77, "acc_user")
    msgs = [
        _FakeMsg(1, "Продам гараж, реклама не нужна", human),  # болтовня: не нам и не бот
        _FakeMsg(2, "мой пост", me, out=True),
        _FakeMsg(3, "@acc_user автоматическая рассылка запрещена, мут 24ч", bot, reply_to=2),
        _FakeMsg(4, "Спам-бот: пользователь ограничен за спам", bot),  # про наказание, без отметки
        _FakeMsg(
            5,
            "GLOBAL SERVICE CHAT Protection - @WsGuardBot Rules - https://telegra.ph/Rules-GSC",
            bot,
        ),  # шапка чата: ни к кому не относится
    ]
    got = await gather(
        _FakeTg(msgs, mentions={3}),
        object(),
        Ident(user_id=77, username="acc_user"),
        self_reader=True,
        extra_bots={"WsGuardBot"},
    )
    ids = {m.id: m for m in got.messages}
    assert set(ids) == {3, 4}
    assert ids[3].to_me and ids[3].is_bot and not ids[4].to_me
    why = build_why("mute", gathered=got, detected=datetime.now(timezone.utc))
    assert why.cause == "antispam" and why.certain and "автоматическая рассылка" in why.summary


async def test_gather_reports_unreadable_chat():
    from app.tg.mutewhy import gather

    class Broken(_FakeTg):
        async def iter_messages(self, entity, limit=None, filter=None, from_user=None):
            raise RuntimeError("CHANNEL_PRIVATE")
            yield  # pragma: no cover

    got = await gather(Broken([]), object(), Ident(user_id=1), self_reader=True)
    assert not got.readable and "CHANNEL_PRIVATE" in got.read_error
