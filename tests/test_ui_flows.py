from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.context import ctx
from app.jobs.advisor import build_advice, run_advisor
from app.jobs.chatprefs import cycle_mention, list_account_items, set_enabled
from app.jobs.coverage import build_coverage, coverage_report
from app.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "ui.db")
    await db.init()
    ctx.store = db
    return db


async def _seed(store: Store):
    accs = []
    for i in range(4):
        a = await store.add_account(f"acc{i}")
        await store.update_account(a.id, telethon_session=f"/tmp/s{i}.session")
        accs.append(await store.get_account(a.id))
    chat = await store.add_chat("Get-CHAT УСЛУГИ", "-1001000000001", kind="schedule")
    other = await store.add_chat("Mamont", "-1001000000002", kind="schedule")
    return accs, chat, other


async def test_coverage_counts_working_banned_and_needed(store: Store):
    accs, chat, other = await _seed(store)
    await store.set_slot(chat.id, accs[0].id, 0)
    await store.set_slot(chat.id, accs[1].id, 20)
    await store.record_setup_ok(accs[0].id, chat.id)
    await store.record_setup_ok(accs[1].id, chat.id)
    await store.upsert_restriction(accs[2].id, chat.id, "ban")
    await store.upsert_restriction(accs[3].id, chat.id, "mute", until_at="")
    cov = {c.title: c for c in await build_coverage(store)}
    get = cov["Get-CHAT УСЛУГИ"]
    assert len(get.working) == 2 and get.banned == ["acc2"] and get.muted == ["acc3"]
    assert get.needed == 12 and get.deficit == 10 and get.gap == 40
    text = await coverage_report(store)
    assert "Get-CHAT" in text and "нужно ещё 10" in text


async def test_advice_flags_account_blocked_everywhere(store: Store):
    accs, chat, other = await _seed(store)
    for c in (chat, other):
        await store.upsert_restriction(accs[0].id, c.id, "ban")
    await store.update_account(accs[0].id, spam_status="limited", spam_until="11 Oct 2026")
    adv = {a.account.label: a for a in await build_advice(store)}
    assert adv["acc0"].level == "stop"
    text = adv["acc0"].text("@arxixx")
    assert "@arxixx" in text and "вреда" in text and "SpamBot" in text
    assert adv["acc1"].level == "ok"
    sent: list[str] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    out = await run_advisor(store, FakeBot(), 1, force=True)
    assert "acc0" in out and sent
    again = await run_advisor(store, FakeBot(), 1)
    assert "новых сообщений нет" in again


async def test_account_chat_items_and_toggle(store: Store):
    accs, chat, other = await _seed(store)
    await store.save_chat_post(accs[0].id, chat.chat_id, "свой", [], "")
    items = await list_account_items(store, accs[0], include_live=False)
    get = next(i for i in items if i.chat is not None and i.chat.id == chat.id)
    assert get.enabled and get.has_text and get.mention_label == "—"
    # без Telethon-сессии (в тесте сети нет): переключатель не должен падать
    await store.update_account(accs[0].id, telethon_session="")
    acc0 = await store.get_account(accs[0].id)
    note = await set_enabled(store, acc0, get, False)
    pref = await store.get_pref(accs[0].id, chat.chat_id)
    assert not pref.is_enabled
    assert (accs[0].id, chat.id) not in {(s.account_id, s.chat_pk) for s in await store.all_slots()}
    assert "отложенные" in note or note == ""
    msg = await cycle_mention(store, accs[0], get)
    assert "sender" in msg.lower()


async def test_schedule_skips_disabled_and_banned(store: Store):
    from app.jobs import LogSink
    from app.jobs.setup import pair_blocked

    accs, chat, other = await _seed(store)
    assert await pair_blocked(store, accs[0], chat) is None
    await store.set_pref(accs[0].id, chat.chat_id, enabled=False)
    assert await pair_blocked(store, accs[0], chat) == "disabled"
    await store.set_pref(accs[0].id, chat.chat_id, enabled=True)
    await store.upsert_restriction(accs[0].id, chat.id, "mute")
    assert await pair_blocked(store, accs[0], chat) == "muted"
    await store.upsert_restriction(accs[0].id, chat.id, "ban")
    assert await pair_blocked(store, accs[0], chat) == "banned"


def test_spam_label_and_restriction_classifier():
    from app.tg.restrictions import classify_text

    assert classify_text("UserBannedInChannelError: You're banned from sending messages") == "ban"
    assert classify_text("ChatWriteForbiddenError: You can't write in this chat") == "mute"
    assert classify_text("чат не найден в аккаунте") is None


async def test_account_chats_screen_renders(store: Store):
    from app.bot.handlers.acc_chats import _screen

    accs, chat, other = await _seed(store)
    await store.upsert_restriction(accs[0].id, other.id, "mute", until_at="2026-10-09T10:00:00+00:00", reason="спам")
    await store.set_pref(accs[0].id, chat.chat_id, enabled=False)
    built = await _screen(accs[0].id, 0)
    assert built is not None
    text, markup = built
    assert "Get-CHAT" in text and "мут до" in text and "⛔" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert all(len(c.encode()) <= 64 for c in callbacks)
    assert any(c.startswith("ac:t:") for c in callbacks)


async def test_collect_chat_sends_reads_own_messages(store: Store):
    from types import SimpleNamespace

    from app.jobs.facts import collect_chat_sends

    accs, chat, _ = await _seed(store)
    now = datetime.now(timezone.utc)
    msgs = [
        SimpleNamespace(id=30, date=now - timedelta(minutes=5)),
        SimpleNamespace(id=29, date=now - timedelta(minutes=65)),
        SimpleNamespace(id=28, date=now - timedelta(hours=5)),  # старше окна → стоп
        SimpleNamespace(id=27, date=now - timedelta(hours=6)),
    ]

    class FakeClient:
        async def _gen(self):
            for m in msgs:
                yield m

        def iter_messages(self, entity, **kw):
            assert kw.get("from_user") == "me"
            return self._gen()

    n = await collect_chat_sends(store, FakeClient(), accs[0], chat, object(), now - timedelta(hours=3))
    assert n == 2
    scans = await store.list_scans()
    assert scans and scans[0][3] == "ok"


async def test_restriction_hint_without_probe_and_account_level_limit(store: Store):
    from app.tg.restrictions import Probe, record_restriction

    accs, chat, _ = await _seed(store)
    # бан по тексту ошибки, проверить нечем → в базу банов
    assert await record_restriction(store, None, accs[0], chat, None, hint="ban", error="x") == "ban"
    await store.resolve_restriction(accs[0].id, chat.id)
    # участник без ограничений + «banned from sending» = лимит аккаунта (SpamBlock),
    # не бан чата: пара закрывается до конца лимита / на сутки
    kind = await record_restriction(
        store, None, accs[1], chat, None, hint="ban", probe=Probe("ok")
    )
    r = await store.get_restriction(accs[1].id, chat.id)
    assert kind == "spamblock" and r and r.is_spamblock and r.until_at
    assert (accs[1].id, chat.id) not in await store.banned_pairs()
    # мут с известным сроком
    until = datetime.now(timezone.utc) + timedelta(days=1)
    kind = await record_restriction(
        store, None, accs[2], chat, None, hint="mute", probe=Probe("mute", until, "ограничен")
    )
    r = await store.get_restriction(accs[2].id, chat.id)
    assert kind == "mute" and r and r.until_at and r.reason == "ограничен"


async def test_notifier_merges_and_retries():
    import asyncio

    from aiogram.exceptions import TelegramRetryAfter

    from app import notify

    calls: list[str] = []
    state = {"fail": 1}

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            if state["fail"]:
                state["fail"] -= 1
                raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
            calls.append(text)

    n = notify.Notifier()
    notify.MERGE_WINDOW, notify.SEND_GAP = 0.05, 0.01
    bot = FakeBot()
    for i in range(5):
        n.push(bot, 1, f"line {i}")
    await n.flush(5)
    await asyncio.sleep(0.1)
    assert calls and "line 0" in calls[0] and "line 4" in "\n".join(calls)
    assert len(calls) <= 2  # пачка склеена
