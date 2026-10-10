import contextvars
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.config import Settings
from app.jobs import autopilot
from app.jobs.spam import SpamCheckResult
from app.store import Store
from app.tg.join import JoinResult, MembershipReport


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "ap.db")
    await db.init()
    return db


@pytest.fixture
def world(monkeypatch):
    """Подмена Telethon: членство и вступление задаются тестом."""

    class World:
        member: set[tuple[str, int]] = set()  # (label, chat_pk) уже в чате
        join_status: dict[tuple[str, int], tuple[str, str]] = {}
        joins: list[tuple[str, int]] = []
        setup_calls: list[tuple[int, list[int]]] = []
        sender_calls: list[int] = []
        _var = contextvars.ContextVar("acc", default="")

        @property
        def current(self) -> str:
            return self._var.get()

        @current.setter
        def current(self, value: str) -> None:
            self._var.set(value)

    w = World()
    w.member, w.join_status, w.joins = set(), {}, []
    w.setup_calls, w.sender_calls = [], []

    cfg = Settings(
        _env_file=None,
        autopilot_join_pause_min_sec=0,
        autopilot_join_pause_max_sec=0,
        autopilot_join_per_tick=3,
    )
    monkeypatch.setattr(autopilot, "get_settings", lambda: cfg)

    @asynccontextmanager
    async def fake_open(acc):
        w.current = acc.label
        yield object()

    async def fake_check(client, chats):
        rep = MembershipReport()
        for chat in chats:
            if (w.current, chat.id) in w.member:
                rep.joined.append(chat)
            elif chat.has_join_link:
                rep.missing.append(chat)
            else:
                rep.no_link.append(chat)
        return rep

    async def fake_join(client, chat, **kw):
        w.joins.append((w.current, chat.id))
        status, detail = w.join_status.get((w.current, chat.id), ("joined", ""))
        if status in {"joined", "captcha_ok", "already"}:
            w.member.add((w.current, chat.id))
        return JoinResult(chat.display_name, chat.id, status, detail)

    async def fake_setup(store, account_id, chat_pks, bot, admin, **kw):
        w.setup_calls.append((account_id, list(chat_pks)))
        titles = [(await store.get_chat(pk)).title for pk in chat_pks]
        for pk in chat_pks:
            await store.record_setup_ok(account_id, pk)
        return {"ok": titles, "skipped": [], "abandoned": [], "config_error": []}

    async def fake_sender(store, account_id, bot, admin, **kw):
        w.sender_calls.append(account_id)
        return 1

    w.spam_events = []
    w.gate_events = []
    w.stopped = []

    async def fake_spam(store, acc, client, **kw):
        res = SpamCheckResult(account=acc, skipped="test")
        res.events = list(w.spam_events)
        w.spam_events = []
        return res

    async def fake_gate(store, client, acc, chats, **kw):
        out, w.gate_events = list(w.gate_events), []
        return out

    async def fake_sweep(store, acc, api=None):
        return list(w.stopped)

    w.perf_events = []

    async def fake_perf(store, client, acc, **kw):
        return w.perf_events.pop(0) if w.perf_events else None

    monkeypatch.setattr(autopilot, "perf_step", fake_perf)
    w.sync_calls = []

    async def fake_sync(store, account_id, bot=None, admin=None):
        w.sync_calls.append(account_id)
        return "ок"

    monkeypatch.setattr(autopilot, "sync_sender_load", fake_sync)
    monkeypatch.setattr(autopilot, "run_spam_check", fake_spam)
    monkeypatch.setattr(autopilot, "gate_step", fake_gate)
    monkeypatch.setattr(autopilot, "sweep_stoplist", fake_sweep)
    monkeypatch.setattr(autopilot, "_open_client", fake_open)
    monkeypatch.setattr(autopilot, "check_membership", fake_check)
    monkeypatch.setattr(autopilot, "join_one", fake_join)
    monkeypatch.setattr(autopilot, "run_setup_chats_only", fake_setup)
    monkeypatch.setattr(autopilot, "run_sender_refresh", fake_sender)
    return w


async def _acc(store: Store, label: str, **fields):
    acc = await store.add_account(label)
    await store.update_account(acc.id, telethon_session=f"/tmp/{label}.session", **fields)
    return await store.get_account(acc.id)


async def test_joins_missing_chat_then_configures_schedule(store, world):
    acc = await _acc(store, "tron")
    chat = await store.add_chat("MARKET 404", "-1001", kind="schedule", invite_link="https://t.me/+abc")
    bot = FakeBot()

    summary = await autopilot.run_autopilot(store, bot, 777, force=True)

    assert world.joins == [("tron", chat.id)]
    assert world.setup_calls == [(acc.id, [chat.id])]
    assert len(bot.sent) == 1 and bot.sent[0][0] == 777
    assert "Вступил" in bot.sent[0][1] and "tron → MARKET 404" in bot.sent[0][1]
    assert "Настроил отправку" in bot.sent[0][1]
    st = await store.get_join_state(acc.id, chat.id)
    assert st.status == "member"
    assert summary


async def test_nothing_to_do_is_silent(store, world):
    await _acc(store, "tron")
    chat = await store.add_chat("C", "-1001", kind="schedule", invite_link="https://t.me/+abc")
    world.member.add(("tron", chat.id))
    # настройка уже была
    acc = (await store.list_accounts())[0]
    await store.record_setup_ok(acc.id, chat.id)
    bot = FakeBot()

    summary = await autopilot.run_autopilot(store, bot, 777, force=True)

    assert bot.sent == []
    assert world.joins == [] and world.setup_calls == []
    assert "всё в порядке" in summary


async def test_member_without_setup_gets_configured_without_joining(store, world):
    acc = await _acc(store, "tron")
    chat = await store.add_chat("LSA", "-1001", kind="schedule", invite_link="https://t.me/+abc")
    world.member.add(("tron", chat.id))
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 777, force=True)

    assert world.joins == []
    assert world.setup_calls == [(acc.id, [chat.id])]


async def test_disabled_pair_and_banned_pair_are_left_alone(store, world):
    acc = await _acc(store, "tron")
    c_off = await store.add_chat("Off", "-1001", invite_link="https://t.me/+a")
    c_ban = await store.add_chat("Banned", "-1002", invite_link="https://t.me/+b")
    c_ok = await store.add_chat("Ok", "-1003", invite_link="https://t.me/+c")
    await store.set_chat_send_enabled(acc.id, c_off.id, False)
    await store.record_ban(acc.id, c_ban.id, "join_ban")
    await store.mark_ban_notified([(await store.get_ban(acc.id, c_ban.id)).id])

    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)

    assert world.joins == [("tron", c_ok.id)]


async def test_join_ban_is_recorded_and_reported_once(store, world):
    acc = await _acc(store, "tron")
    chat = await store.add_chat("MARKET 404", "-1001", invite_link="https://t.me/+abc")
    world.join_status[("tron", chat.id)] = (
        "failed",
        "UserBannedInChannelError: You're banned from sending messages",
    )
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 777, force=True)

    ban = await store.get_ban(acc.id, chat.id)
    assert ban is not None and ban.active and ban.reason == "join_ban" and ban.notified == 1
    texts = "\n".join(t for _, t in bot.sent)
    assert "Новые баны" in texts and "MARKET 404" in texts and "tron" in texts
    assert "Рекомендация" in texts

    # следующий проход: пара забанена → автопилот её не трогает и молчит
    bot2 = FakeBot()
    world.joins.clear()
    await autopilot.run_autopilot(store, bot2, 777, force=True)
    assert world.joins == []
    assert bot2.sent == []


async def test_flood_stops_account_and_does_not_count_as_failure(store, world):
    acc = await _acc(store, "tron")
    c1 = await store.add_chat("A", "-1001", invite_link="https://t.me/+a")
    c2 = await store.add_chat("B", "-1002", invite_link="https://t.me/+b")
    world.join_status[("tron", c1.id)] = ("failed", "FloodWait 40s")
    world.join_status[("tron", c2.id)] = ("failed", "FloodWait 40s")

    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)

    assert len(world.joins) == 1  # после FloodWait аккаунт больше не дёргаем
    st = await store.get_join_state(acc.id, world.joins[0][1])
    assert st.fail_count == 0 and st.status == "pending"


async def test_join_budget_per_tick(store, world):
    await _acc(store, "tron")
    for i in range(5):
        await store.add_chat(f"C{i}", f"-100{i}", invite_link=f"https://t.me/+h{i}")

    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)
    assert len(world.joins) == 3  # autopilot_join_per_tick

    world.joins.clear()
    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)
    assert len(world.joins) == 2  # остальные на следующем проходе


async def test_failed_join_backoff_then_manual_for_bad_invite(store, world):
    acc = await _acc(store, "tron")
    chat = await store.add_chat("Dead invite", "-1001", invite_link="https://t.me/+abc")
    world.join_status[("tron", chat.id)] = ("failed", "инвайт недействителен: InviteHashExpiredError")
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 1, force=True)

    st = await store.get_join_state(acc.id, chat.id)
    assert st.status == "manual"
    assert "Нужна помощь" in bot.sent[0][1] and "Dead invite" in bot.sent[0][1]

    world.joins.clear()
    bot2 = FakeBot()
    await autopilot.run_autopilot(store, bot2, 1, force=True)
    assert world.joins == [] and bot2.sent == []  # не долбим, пока не сбросите вручную

    await store.reset_join_states()
    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)
    assert world.joins == [("tron", chat.id)]


async def test_needs_manual_when_no_way_to_join(store, world):
    await _acc(store, "tron")
    await store.add_chat("Nowhere", "nowhere")  # ни ссылки, ни username
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 1, force=True)

    assert world.joins == []
    assert "Nowhere" in bot.sent[0][1] and "нет ссылки" in bot.sent[0][1]
    bot2 = FakeBot()
    await autopilot.run_autopilot(store, bot2, 1, force=True)
    assert bot2.sent == []  # сообщили один раз


async def test_removed_from_chat_confirmed_by_two_checks(store, world):
    acc = await _acc(store, "tron")
    chat = await store.add_chat("MARKET 404", "-1001", invite_link="https://t.me/+abc")
    await store.record_setup_ok(acc.id, chat.id)
    world.member.add(("tron", chat.id))

    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)  # был в чате
    world.member.clear()

    bot = FakeBot()
    await autopilot.run_autopilot(store, bot, 1, force=True)  # 1-й пропуск
    assert await store.get_ban(acc.id, chat.id) is None
    assert world.joins == []  # не лезем обратно в чат, из которого, возможно, выкинули
    assert bot.sent == []

    bot = FakeBot()
    await autopilot.run_autopilot(store, bot, 1, force=True)  # 2-й пропуск — вылет подтверждён
    ban = await store.get_ban(acc.id, chat.id)
    assert ban is not None and ban.reason == "removed" and ban.active
    text = "\n".join(t for _, t in bot.sent)
    assert "Вылетели" in text and "Новые баны" in text
    assert world.joins == []


async def test_returning_member_clears_removed_ban(store, world):
    acc = await _acc(store, "tron")
    chat = await store.add_chat("C", "-1001", invite_link="https://t.me/+abc")
    await store.record_ban(acc.id, chat.id, "removed")
    await store.clear_ban(acc.id, chat.id)  # админ снял метку вручную
    await store.reset_pair_join(acc.id, chat.id)
    world.member.add(("tron", chat.id))
    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)
    assert await store.active_ban_pairs() == set()


async def test_sender_chat_triggers_sender_refresh_only_for_sender_account(store, world):
    a1 = await _acc(store, "with_sender", sender_account_id="sid1")
    await _acc(store, "no_sender")
    chat = await store.add_chat("Услуги", "-1001", kind="sender", invite_link="https://t.me/+abc")

    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)

    assert set(world.joins) == {("with_sender", chat.id), ("no_sender", chat.id)}
    assert world.sender_calls == [a1.id]
    assert world.setup_calls == []  # sender-чаты не планируются через schedule


async def test_sender_off_account_still_joins_but_not_refreshed(store, world):
    await _acc(store, "off", sender_account_id="sid1", sender_enabled=0)
    chat = await store.add_chat("Услуги", "-1001", kind="sender", invite_link="https://t.me/+abc")

    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)

    assert world.joins == [("off", chat.id)]
    assert world.sender_calls == []


async def test_disabled_autopilot_does_nothing_unless_forced(store, world):
    await _acc(store, "tron")
    await store.add_chat("C", "-1001", invite_link="https://t.me/+abc")
    await store.set_setting("autopilot_enabled", "0")

    assert await autopilot.run_autopilot(store, FakeBot(), 1) == "Автопилот выключен"
    assert world.joins == []
    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)
    assert len(world.joins) == 1


async def test_busy_account_is_skipped(store, world, monkeypatch):
    await _acc(store, "tron")
    await store.add_chat("C", "-1001", invite_link="https://t.me/+abc")
    monkeypatch.setattr(
        autopilot.runtime, "is_running", lambda kind, aid: kind == "setup" and aid != 0
    )
    await autopilot.run_autopilot(store, FakeBot(), 1, force=True)
    assert world.joins == []


async def test_one_account_error_does_not_break_others(store, world, monkeypatch):
    await _acc(store, "bad")
    await _acc(store, "good")
    chat = await store.add_chat("C", "-1001", invite_link="https://t.me/+abc")
    real_check = autopilot.check_membership

    async def flaky(client, chats):
        if world.current == "bad":
            raise RuntimeError("session revoked")
        return await real_check(client, chats)

    monkeypatch.setattr(autopilot, "check_membership", flaky)
    bot = FakeBot()
    await autopilot.run_autopilot(store, bot, 1, force=True)

    assert ("good", chat.id) in world.joins
    text = "\n".join(t for _, t in bot.sent)
    assert "bad" in text and "session revoked" in text


# ---- спамблок / dead / стоп-лист / ворота -----------------------------------


from datetime import datetime, timedelta, timezone  # noqa: E402

from app.jobs.gate import GateEvent  # noqa: E402


def _limited_since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


async def test_spamblock_lowers_load_and_only_open_chats_wait(store, world):
    acc = await _acc(store, "tron", sender_account_id="sid1")
    closed = await store.add_chat("MARKET 404", "-1001", invite_link="https://t.me/+abc")
    opened = await store.add_chat("Открытый", "-1002", username="open_chat")
    st = await store.get_spam_state(acc.id)
    st.status, st.limited_since, st.strikes = "limited", _limited_since(0.1), 1
    await store.save_spam_state(st)
    world.spam_events = ["limited_new"]
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    # закрытый чат (инвайт) в спамблоке доступен — вступаем; открытый ждёт снятия блока
    assert [c for _, c in world.joins] == [closed.id]
    assert world.sync_calls == [acc.id]  # нагрузка sender 100% → 50%
    text = bot.sent[0][1]
    assert "Спамблок" in text and "tron" in text and "schedule не трогаю" in text
    assert "100% → 50%" in text

    # блок снят — открытый чат вступается
    st.status = "clean"
    await store.save_spam_state(st)
    world.joins.clear()
    await autopilot.run_autopilot(store, FakeBot(), 5, force=True)
    assert [c for _, c in world.joins] == [opened.id]


async def test_ramp_down_step_without_new_check(store, world):
    acc = await _acc(store, "tron", sender_account_id="sid1")
    await store.add_chat("C", "-1001", invite_link="https://t.me/+abc")
    st = await store.get_spam_state(acc.id)
    st.status, st.limited_since, st.applied_load = "limited", _limited_since(8), 50
    await store.save_spam_state(st)
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    assert world.sync_calls == [acc.id]
    assert "50% → 25%" in bot.sent[0][1]
    # ступень уже применена — следующий проход молчит
    world.sync_calls.clear()
    await store.set_applied_load(acc.id, 25)
    bot2 = FakeBot()
    await autopilot.run_autopilot(store, bot2, 5, force=True)
    assert world.sync_calls == [] and bot2.sent == []


async def test_dead_event_is_announced_and_sender_stopped_once(store, world):
    acc = await _acc(store, "tron", sender_account_id="sid1")
    await store.add_chat("C", "-1001", invite_link="https://t.me/+abc")
    await store.update_account(acc.id, dead=1)
    st = await store.get_spam_state(acc.id)
    st.strikes = 3
    await store.save_spam_state(st)
    world.spam_events = ["limited_new", "dead"]
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    assert world.sync_calls == [acc.id]
    assert "dead" in bot.sent[0][1] and "только schedule" in bot.sent[0][1]
    await store.set_applied_load(acc.id, 0)
    world.sync_calls.clear()
    await autopilot.run_autopilot(store, FakeBot(), 5, force=True)
    assert world.sync_calls == []


async def test_dead_account_still_joins_and_keeps_schedule(store, world):
    acc = await _acc(store, "tron", sender_account_id="sid1", dead=1)
    await store.set_applied_load(acc.id, 0)
    sched = await store.add_chat("Sched", "-1001", kind="schedule", invite_link="https://t.me/+a")
    sender = await store.add_chat("Send", "-1002", kind="sender", invite_link="https://t.me/+b")

    await autopilot.run_autopilot(store, FakeBot(), 5, force=True)

    assert {c for _, c in world.joins} == {sched.id, sender.id}
    assert world.setup_calls == [(acc.id, [sched.id])]  # schedule настроен
    assert world.sender_calls == []  # sender не трогаем


async def test_spamblock_cleared_is_reported(store, world):
    acc = await _acc(store, "tron", sender_account_id="sid1")
    await store.add_chat("C", "-1001", invite_link="https://t.me/+abc")
    st = await store.get_spam_state(acc.id)
    st.cleared_at, st.applied_load = datetime.now(timezone.utc).isoformat(), 0
    await store.save_spam_state(st)
    world.spam_events = ["cleared"]
    bot = FakeBot()
    await autopilot.run_autopilot(store, bot, 5, force=True)
    assert "спамблок снят" in bot.sent[0][1] and "0% → 50%" in bot.sent[0][1]


async def test_no_post_chats_are_not_joined_or_configured(store, world):
    await _acc(store, "tron", sender_account_id="sid1")
    ok = await store.add_chat("Услуги", "-1001", invite_link="https://t.me/+a")
    await store.add_chat("Отзывы клиентов", "-1002", invite_link="https://t.me/+b")  # стоп-лист
    flagged = await store.add_chat("Тихий чат", "-1003", invite_link="https://t.me/+c")
    await store.update_chat(flagged.id, no_post=1)

    await autopilot.run_autopilot(store, FakeBot(), 5, force=True)

    assert [c for _, c in world.joins] == [ok.id]


async def test_gate_and_stopped_reports(store, world):
    await _acc(store, "tron", sender_account_id="sid1")
    chat = await store.add_chat("Услуги", "-1001", invite_link="https://t.me/+a")
    world.member.add(("tron", chat.id))
    acc = (await store.list_accounts())[0]
    await store.record_setup_ok(acc.id, chat.id)
    world.gate_events = [
        GateEvent("Услуги", "resolved", "@news_channel, нажал «Я подписался»"),
        GateEvent("Упрямый", "unresolved", "бот снова требует подписку"),
    ]
    world.stopped = ["Отзывы клиентов"]
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    text = bot.sent[0][1]
    assert "Подписался на обязательные каналы" in text and "@news_channel" in text
    assert "Нужна помощь" in text and "Упрямый" in text
    assert "Выключил чаты «писать нельзя»" in text and "Отзывы клиентов" in text


# ---- аутрич-аккаунты: мягкий режим ------------------------------------------


async def test_outreach_joins_only_schedule_chats_and_never_touches_sender(store, world):
    acc = await _acc(store, "out1", sender_account_id="sid1", outreach=1)
    sched = await store.add_chat("Sched", "-1001", kind="schedule", invite_link="https://t.me/+a")
    await store.add_chat("Sender-чат", "-1002", kind="sender", invite_link="https://t.me/+b")
    await store.set_applied_load(acc.id, 0)  # sender уже остановлен
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    assert [c for _, c in world.joins] == [sched.id]  # sender-чат не трогаем
    assert world.setup_calls == [(acc.id, [sched.id])]
    assert world.sender_calls == [] and world.sync_calls == []


async def test_outreach_first_pass_stops_sender_quietly(store, world):
    acc = await _acc(store, "out1", sender_account_id="sid1", outreach=1)
    await store.add_chat("Sched", "-1001", kind="schedule", invite_link="https://t.me/+a")
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    assert world.sync_calls == [acc.id]  # один раз выключили sender в Autoposter
    assert "нагрузка sender" not in bot.sent[0][1]  # и не шумим про это


async def test_outreach_spamblock_events_do_not_make_noise(store, world):
    from app.jobs import autopilot as ap

    acc = await _acc(store, "out1", outreach=1)
    await store.add_chat("Sched", "-1001", kind="schedule", invite_link="https://t.me/+a")
    world.member.add(("out1", 1))
    await store.record_setup_ok(acc.id, 1)
    st = await store.get_spam_state(acc.id)
    st.status, st.limited_since, st.strikes = "limited", _limited_since(1), 5
    await store.save_spam_state(st)
    world.spam_events = ["limited_new"]  # даже если бы событие пришло — для аутрича спам-шума нет
    bot = FakeBot()

    summary = await ap.run_autopilot(store, bot, 5, force=True)

    assert bot.sent == [] and "всё в порядке" in summary
    assert not (await store.get_account(acc.id)).is_dead


async def test_spamblock_does_not_stop_schedule_in_closed_chats(store, world):
    """Закрытые чаты спамблок не ограничивает — schedule настраивается как обычно."""
    acc = await _acc(store, "tron")
    closed = await store.add_chat("Closed", "-1001", kind="schedule", invite_link="https://t.me/+a")
    opened = await store.add_chat("Open", "-1002", kind="schedule", username="open_chat")
    world.member.update({("tron", closed.id), ("tron", opened.id)})
    st = await store.get_spam_state(acc.id)
    st.status, st.limited_since, st.applied_load = "limited", _limited_since(1), 50
    await store.save_spam_state(st)

    await autopilot.run_autopilot(store, FakeBot(), 5, force=True)
    assert world.setup_calls == [(acc.id, [closed.id])]  # открытый ждёт, закрытый — работает

    st.status = "clean"
    await store.save_spam_state(st)
    await store.set_applied_load(acc.id, 100)
    await autopilot.run_autopilot(store, FakeBot(), 5, force=True)
    assert world.setup_calls[-1] == (acc.id, [opened.id])


async def test_outreach_in_spamblock_keeps_working_in_closed_schedule_chats(store, world):
    acc = await _acc(store, "out", outreach=1)
    await store.set_applied_load(acc.id, 0)
    chat = await store.add_chat("Closed", "-1001", kind="schedule", invite_link="https://t.me/+a")
    st = await store.get_spam_state(acc.id)
    st.status, st.limited_since, st.strikes = "limited", _limited_since(30), 9
    await store.save_spam_state(st)
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    assert [c for _, c in world.joins] == [chat.id]  # вступил, несмотря на спамблок
    assert world.setup_calls == [(acc.id, [chat.id])]  # и schedule настроен
    assert not (await store.get_account(acc.id)).is_dead


async def test_regular_and_outreach_accounts_together(store, world):
    reg = await _acc(store, "reg", sender_account_id="sid1")
    await _acc(store, "out", outreach=1)
    sched = await store.add_chat("Sched", "-1001", kind="schedule", invite_link="https://t.me/+a")
    sender = await store.add_chat("Send", "-1002", kind="sender", invite_link="https://t.me/+b")

    await autopilot.run_autopilot(store, FakeBot(), 5, force=True)

    assert set(world.joins) == {
        ("reg", sched.id), ("reg", sender.id), ("out", sched.id),
    }
    assert world.sender_calls == [reg.id]


# ---- результативность: «аккаунты для переоформления» -------------------------

from app.jobs.perf import PerfEvent  # noqa: E402
from app.models import AccountPerf  # noqa: E402


def _perf_event(acc, kind, wrote=0, new=0, cloak=0):
    perf = AccountPerf(account_id=acc.id, checked_at="x", wrote_n=wrote, new_n=new, cloak_n=cloak,
                       window_days=7, flagged_at="x")
    return PerfEvent(acc, perf, kind)


async def test_unproductive_account_is_announced_with_mention(store, world):
    acc = await _acc(store, "tron")
    chat = await store.add_chat("C", "-1001", invite_link="https://t.me/+a")
    world.member.add(("tron", chat.id))
    await store.record_setup_ok(acc.id, chat.id)
    world.perf_events = [_perf_event(acc, "flagged", wrote=1, new=0, cloak=0)]
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    text = bot.sent[0][1]
    assert text.startswith("@arxixx аккаунты не приносят клиентов — нужно переоформить: 1")
    assert "Аккаунты для переоформления" in text
    assert "tron: написали 1 чел. за 7 дн." in text and "клоакинг сработал: 0" in text


async def test_recovered_account_is_good_news_without_mention(store, world):
    acc = await _acc(store, "tron")
    chat = await store.add_chat("C", "-1001", invite_link="https://t.me/+a")
    world.member.add(("tron", chat.id))
    await store.record_setup_ok(acc.id, chat.id)
    world.perf_events = [_perf_event(acc, "recovered", wrote=9)]
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    text = bot.sent[0][1]
    assert "Хорошие новости" in text and "снова приносит клиентов" in text
    assert "@arxixx" not in text and "Аккаунты для переоформления" not in text


async def test_perf_failure_does_not_break_account_pass(store, world, monkeypatch):
    await _acc(store, "tron")
    chat = await store.add_chat("C", "-1001", invite_link="https://t.me/+a")

    async def boom(store, client, acc, **kw):
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(autopilot, "perf_step", boom)
    bot = FakeBot()
    await autopilot.run_autopilot(store, bot, 5, force=True)

    assert [c for _, c in world.joins] == [chat.id]  # вступление отработало
    assert "оценка результативности" in bot.sent[0][1]


async def test_several_unproductive_accounts_counted_in_one_mention(store, world):
    a = await _acc(store, "a1")
    b = await _acc(store, "a2")
    chat = await store.add_chat("C", "-1001", invite_link="https://t.me/+a")
    for acc in (a, b):
        world.member.add((acc.label, chat.id))
        await store.record_setup_ok(acc.id, chat.id)
    world.perf_events = [_perf_event(a, "flagged"), _perf_event(b, "reminder")]
    bot = FakeBot()
    await autopilot.run_autopilot(store, bot, 5, force=True)
    assert bot.sent[0][1].startswith("@arxixx аккаунты не приносят клиентов — нужно переоформить: 2")


async def test_link_found_by_member_wakes_others_who_could_not_join(store, world, monkeypatch):
    """Чат известен только по id: аккаунт, который в нём, отдаёт ссылку — остальные вступают."""
    from types import SimpleNamespace

    from app.tg.linkharvest import HarvestedLink

    insider = await _acc(store, "insider")
    outsider = await _acc(store, "outsider")
    chat = await store.add_chat("GSC | ЧАТ | WS Project", "-1001995593406", kind="schedule")
    world.member.add(("insider", chat.id))
    await store.record_setup_ok(insider.id, chat.id)

    async def fake_lookup(client, c):
        return SimpleNamespace(id=1)

    async def fake_harvest(client, entity):
        return HarvestedLink(invite="https://t.me/+found")

    monkeypatch.setattr(autopilot, "lookup_entity", fake_lookup)
    monkeypatch.setattr(autopilot, "harvest_join_link", fake_harvest)
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)
    assert (await store.get_chat(chat.id)).invite_link == "https://t.me/+found"
    st = await store.get_join_state(outsider.id, chat.id)
    assert st.status in {"manual", "member", "pending"}

    await autopilot.run_autopilot(store, bot, 5, force=True)
    assert ("outsider", chat.id) in world.joins
    assert (await store.get_join_state(outsider.id, chat.id)).status == "member"
    assert any("взял" in text and "вступят сами" in text for _id, text in bot.sent)


async def test_spamblock_with_sender_is_switched_to_dead_not_just_reported(store, world):
    acc = await _acc(store, "faraon", spam_status="limited", sender_account_id="s-1")
    chat = await store.add_chat("C", "-1001", kind="schedule", invite_link="https://t.me/+a")
    world.member.add(("faraon", chat.id))
    await store.record_setup_ok(acc.id, chat.id)
    bot = FakeBot()

    await autopilot.run_autopilot(store, bot, 5, force=True)

    fresh = await store.get_account(acc.id)
    assert fresh.is_dead and fresh.sender_forbidden
    assert acc.id in world.sync_calls  # sender в Autoposter остановлен
    assert any("перевёл в dead" in text for _id, text in bot.sent)