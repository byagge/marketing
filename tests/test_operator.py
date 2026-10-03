from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.operator import engine
from app.operator.diagnose import (
    ACCOUNT_SPAM,
    BANNED,
    JOIN,
    MUTED,
    NO_DATA,
    NOT_MEMBER,
    NOT_MEMBER_NO_LINK,
    NOT_SCHEDULED,
    RESETUP,
    SETUP,
    SILENT,
    WARMING,
    WORKING,
    PairFacts,
    diagnose_pair,
)
from app.store import Store


def pf(**kw) -> PairFacts:
    base = dict(
        account_id=1,
        account_label="alex",
        chat_pk=1,
        chat_title="Get-CHAT",
        has_slot=True,
        setup_status="ok",
        setup_age_min=600,
        scan_status="ok",
        scan_age_min=20,
        sends_3h=0,
    )
    base.update(kw)
    return PairFacts(**base)


def test_diagnose_decision_tree():
    assert diagnose_pair(pf(sends_3h=2)).cause == WORKING
    assert diagnose_pair(pf(restriction="ban")).cause == BANNED
    assert diagnose_pair(pf(restriction="mute")).cause == MUTED
    assert diagnose_pair(pf(scan_age_min=400)).cause == NO_DATA
    assert diagnose_pair(pf(scan_status=None, scan_age_min=None)).cause == NO_DATA
    d = diagnose_pair(pf(scan_status="not_member"))
    assert d.cause == NOT_MEMBER and d.action == JOIN
    d = diagnose_pair(pf(scan_status="not_member", has_join_method=False))
    assert d.cause == NOT_MEMBER_NO_LINK and d.human and "invite" in d.todo
    d = diagnose_pair(pf(has_slot=False, setup_status="abandoned", setup_error="чат не найден"))
    assert d.cause == NOT_SCHEDULED and d.action == SETUP
    assert diagnose_pair(pf(setup_age_min=30)).cause == WARMING
    d = diagnose_pair(pf())
    assert d.cause == SILENT and d.action == RESETUP
    # аккаунт с лимитом SpamBot: не отказываемся, а пробуем (настроить/пересоздать)
    d = diagnose_pair(pf(account_spam="limited", has_slot=False, setup_status=None))
    assert d.cause == NOT_SCHEDULED and d.action == SETUP
    assert diagnose_pair(pf(account_spam="limited", sends_3h=1)).cause == WORKING
    # закрыто лимитом — ждём; срок вышел → перепроверяем и продолжаем
    d = diagnose_pair(pf(restriction="spamblock", restriction_until="2026-10-05 10:00"))
    assert d.cause == "spamblocked" and d.action is None
    d = diagnose_pair(pf(restriction="spamblock", restriction_expired=True))
    assert d.action == "collect"
    d = diagnose_pair(pf(restriction="mute", restriction_until="2026-10-05 10:00"))
    assert d.cause == MUTED and d.action is None  # до срока ничего не делаем
    d = diagnose_pair(pf(restriction="mute", restriction_expired=True))
    assert d.cause == MUTED and d.action == "collect"
    # бан: никаких действий вообще, даже если не в чате
    d = diagnose_pair(pf(restriction="ban", scan_status="not_member"))
    assert d.cause == BANNED and d.action is None
    # выключено вручную и sender-чаты не оцениваем
    assert diagnose_pair(pf(pref_enabled=False)).cause == "disabled"
    assert diagnose_pair(pf(chat_kind="sender")).cause == "sender"


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "op.db")
    await db.init()
    return db


async def _seed(store: Store):
    accs = []
    for i in range(3):
        a = await store.add_account(f"acc{i}")
        await store.update_account(a.id, telethon_session=f"/x/s{i}.session")
        accs.append(await store.get_account(a.id))
    chat = await store.add_chat(
        "Get-CHAT", "-1001000000001", kind="schedule", invite_link="https://t.me/+AAAAAAAAAAAA"
    )
    nolink = await store.add_chat("Скамный Суд", "-1001000000002", kind="schedule")
    return accs, chat, nolink


async def test_cycle_acts_with_cooldown_and_limits(store: Store, monkeypatch):
    accs, chat, nolink = await _seed(store)
    now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    # acc0: в чате, настроено, но молчит; acc1: не в чате (есть invite); acc2: пишет
    for a in accs:
        await store.mark_scan(a.id, chat.id, "ok" if a.id != accs[1].id else "not_member")
        await store.mark_scan(a.id, nolink.id, "not_member")
        await store.set_setting(f"facts_status:{a.id}", f"ok|{iso}|")
    for a in (accs[0], accs[2]):
        await store.set_slot(chat.id, a.id, 5 + a.id)
        await store.record_setup_ok(a.id, chat.id)
        await store.update_account(a.id)  # noop
    # старое «setup ok» — чтобы не было прогрева
    import aiosqlite

    async with aiosqlite.connect(store.path) as db:
        await db.execute("UPDATE setup_states SET last_attempt_at=?", ((now - timedelta(hours=9)).isoformat(),))
        await db.commit()
    await store.add_send_events([(accs[2].id, chat.id, (now - timedelta(minutes=20)).isoformat(timespec="seconds"), 1)])

    calls: list[tuple[str, int, list[int]]] = []

    async def fake(action):
        async def run(st, acc_id, pks):
            calls.append((action, acc_id, pks))
            return True, "ok"
        return run

    for name in (JOIN, SETUP, RESETUP):
        monkeypatch.setitem(engine.EXECUTORS, name, await fake(name))

    res = await engine.run_cycle(store, None, None, act=True)
    acts = {(a, acc) for a, acc, _p in calls}
    assert (RESETUP, accs[0].id) in acts          # молчит → пересоздать
    assert (JOIN, accs[1].id) in acts             # не в чате с invite → вступить
    assert all(acc != accs[2].id or a != RESETUP for a, acc, _ in calls)  # пишущий не трогаем
    # Скамный Суд без ссылки → человеку, а не бесконечные попытки
    assert any("Скамный Суд" in e.title and "invite" in e.todo for e in res.escalations)
    assert res.by_cause["working"] == 1
    # те же пары в следующем цикле: кулдаун — действий нет
    calls.clear()
    await engine.run_cycle(store, None, None, act=True)
    assert calls == []


async def test_exhausted_attempts_escalate_to_human(store: Store, monkeypatch):
    accs, chat, _ = await _seed(store)
    now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    for a in accs:
        await store.set_setting(f"facts_status:{a.id}", f"ok|{iso}|")
        await store.mark_scan(a.id, chat.id, "ok")
        await store.set_slot(chat.id, a.id, 5 + a.id)
        await store.record_setup_ok(a.id, chat.id)
    import aiosqlite

    async with aiosqlite.connect(store.path) as db:
        await db.execute("UPDATE setup_states SET last_attempt_at=?", ((now - timedelta(hours=9)).isoformat(),))
        await db.commit()
    # дневной лимит попыток пересоздания уже исчерпан
    for a in accs:
        await store.set_setting(f"op:{RESETUP}:{a.id}:{chat.id}", f"{iso}|{now.date().isoformat()}|2")

    async def boom(*a, **k):
        raise AssertionError("не должен действовать: попытки исчерпаны")

    monkeypatch.setitem(engine.EXECUTORS, RESETUP, boom)
    res = await engine.run_cycle(store, None, None, act=True)
    e = [x for x in res.escalations if "даже после пересоздания" in x.title]
    assert e and "вручную" in e[0].todo
    incs = await store.list_incidents()
    assert any(i.kind == SILENT and i.source == "operator" for i in incs)


async def test_old_marketer_incidents_closed_once(store: Store):
    await store.upsert_incident(kind="not_assigned", severity="critical", title="выдумка", source="marketer")
    await engine.run_cycle(store, None, None, act=False)
    assert await store.list_incidents() == []


async def test_dm_collector_counts_people_and_incremental(store: Store):
    from app.jobs.dms import collect_dms

    acc = await store.add_account("a")
    now = datetime.now(timezone.utc)

    def msg(i, mins_ago, out=False):
        return SimpleNamespace(id=i, date=now - timedelta(minutes=mins_ago), out=out)

    people = {
        10: SimpleNamespace(id=10, bot=False, is_self=False, deleted=False, username="vasya", first_name="Вася", last_name=None),
        11: SimpleNamespace(id=11, bot=False, is_self=False, deleted=False, username=None, first_name="Петя", last_name=None),
        12: SimpleNamespace(id=12, bot=True, is_self=False, deleted=False, username="somebot", first_name="B", last_name=None),
        777000: SimpleNamespace(id=777000, bot=False, is_self=False, deleted=False, username=None, first_name="Telegram", last_name=None),
    }
    history = {10: [msg(5, 10), msg(4, 600), msg(3, 700, out=False)], 11: [msg(2, 30, out=False), msg(1, 3000, out=True)]}
    firsts = {10: msg(3, 700), 11: msg(1, 3000, out=True)}

    class Client:
        async def _dialogs(self):
            for uid, ent in people.items():
                last = history.get(uid, [msg(1, 5)])[0]
                yield SimpleNamespace(is_user=True, entity=ent, message=last, unread_count=1 if uid == 10 else 0)

        def iter_dialogs(self, limit=None):
            return self._dialogs()

        def iter_messages(self, entity, limit=100, min_id=0):
            async def gen():
                for m in history[entity.id]:
                    if m.id > min_id:
                        yield m
            return gen()

        async def get_messages(self, entity, limit=1, reverse=False, min_id=0):
            return [firsts[entity.id]]

    st = await collect_dms(store, Client(), acc)
    assert st.dialogs == 2 and st.processed == 2 and st.new_people == 1  # бот и 777000 не считаются
    sm = (await store.dm_summary((now - timedelta(hours=24)).isoformat(), (now - timedelta(days=7)).isoformat()))[acc.id]
    assert sm["wrote"] == 2 and sm["first_total"] == 1  # Вася написал первым, Пете первыми написали мы
    assert sm["waiting"] == 1 and sm["active_24h"] == 2
    # второй проход без новых сообщений ничего не пересчитывает (in_count не растёт)
    await collect_dms(store, Client(), acc)
    sm2 = (await store.dm_summary((now - timedelta(hours=24)).isoformat(), (now - timedelta(days=7)).isoformat()))[acc.id]
    assert sm2["wrote"] == 2


async def test_fact_report_uses_only_real_events(store: Store):
    from app.reporting.dashboard import build_dashboard, format_accounts_html, format_dashboard_html, format_density_html
    from app.reporting.factual import build_fact_report, gap_stats

    accs, chat, _ = await _seed(store)
    now = datetime.now(timezone.utc)
    # план есть (слот+setup ok), но реальных сообщений нет → 0 отправок, никакой «оценки по плану»
    await store.set_slot(chat.id, accs[0].id, 5)
    await store.record_setup_ok(accs[0].id, chat.id)
    rep = await build_fact_report(store)
    assert rep.total_day == 0 and rep.chats[0].avg_gap is None and rep.chats[0].working_planned == 1
    # реальные: 3 сообщения за последние 40 минут
    rows = [(accs[0].id, chat.id, (now - timedelta(minutes=m)).isoformat(timespec="seconds"), 100 + m) for m in (5, 20, 38)]
    await store.add_send_events(rows)
    rep = await build_fact_report(store)
    c = rep.chats[0]
    assert rep.total_day == 3 and c.sends_3h == 3 and c.senders_day == 1
    dash = await build_dashboard(store)
    html = format_dashboard_html(dash)
    assert "Get-CHAT" in html and "3" in html
    assert "Реальная частота" in format_density_html(rep)
    text, pages = format_accounts_html(rep, 0)
    assert "acc0" in text and pages == 1
    now_local = now
    avg, med, mx = gap_stats([now - timedelta(minutes=m) for m in (5, 20, 38)], now - timedelta(minutes=60), now)
    assert round(avg) == 20 and mx == 22


async def test_spam_due_picks_stale_and_flagged(store: Store, monkeypatch):
    from app.jobs import spam

    accs, _, _ = await _seed(store)
    now = datetime.now(timezone.utc)
    await store.update_account(accs[0].id, spam_status="clean", spam_checked_at=(now - timedelta(minutes=30)).isoformat())
    await store.update_account(accs[1].id, spam_status="clean", spam_checked_at=(now - timedelta(minutes=30)).isoformat())
    await store.set_setting(f"spam_recheck:{accs[1].id}", now.isoformat())  # подозрение
    # acc2 никогда не проверялся
    checked: list[str] = []

    async def fake_check(st, acc):
        checked.append(acc.label)
        status = "limited" if acc.label == "acc1" else "clean"
        await st.update_account(acc.id, spam_status=status, spam_checked_at=datetime.now(timezone.utc).isoformat())
        return status

    monkeypatch.setattr(spam, "check_account_spam", fake_check)
    sent: list[str] = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    out = await spam.run_spam_due(store, Bot(), 1)
    assert checked == ["acc1", "acc2"]  # подозрение первым, затем непроверенный; acc0 свежий — пропущен
    assert sent and "acc1" in sent[0] and "ограничение" in sent[0]
    assert "проверено 2" in out


def test_parse_spam_until():
    from app.tg.spambot import parse_spam_until

    d = parse_spam_until("Your account is now limited until 11 Oct 2026, 10:32 UTC.")
    assert d == datetime(2026, 10, 11, 10, 32, tzinfo=timezone.utc)
    d = parse_spam_until("ограничения будут сняты до 5 ноя 2026")
    assert d and d.month == 11 and d.day == 5
    assert parse_spam_until("нет даты") is None


def test_needs_count_only_accounts_that_can_write():
    from app.operator.diagnose import Diagnosis
    from app.operator.needs import accounts_to_buy, compute_needs

    diag = []
    for i in range(9):  # 9 из 12 реально могут писать в Get-CHAT
        f = pf(account_id=i, account_label=f"a{i}", sends_3h=1)
        diag.append((f, diagnose_pair(f)))
    for i, kw in enumerate(({"restriction": "ban"}, {"restriction": "spamblock"}, {"pref_enabled": False}), start=20):
        f = pf(account_id=i, account_label=f"b{i}", **kw)
        diag.append((f, diagnose_pair(f)))  # бан / SpamBlock / выкл — не считаются
    f = pf(account_id=30, account_label="c", restriction="mute", restriction_expired=True)
    diag.append((f, diagnose_pair(f)))  # мут закончился — считается
    needs = compute_needs(diag, ["get-chat"])
    n = needs[0]
    assert n.needed == 12 and n.eligible == 10 and n.deficit == 2 and n.priority
    assert accounts_to_buy(needs) == 2


async def test_cycle_asks_for_new_accounts_and_probes_private_first(store: Store, monkeypatch):
    now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    pub = await store.add_chat("Public", "-1001000000010", kind="schedule", username="pubchat")
    priv = await store.add_chat("Private", "-1001000000011", kind="schedule")
    a = await store.add_account("lim")
    await store.update_account(a.id, telethon_session="/x/s.session", spam_status="limited")
    await store.set_setting(f"facts_status:{a.id}", f"ok|{iso}|")
    for c in (pub, priv):
        await store.mark_scan(a.id, c.id, "ok")
    seen: list[list[int]] = []

    async def fake_setup(st, acc_id, pks):
        seen.append(list(pks))
        return True, "ok"

    monkeypatch.setitem(engine.EXECUTORS, SETUP, fake_setup)
    res = await engine.run_cycle(store, None, None, act=True)
    assert seen and seen[0][0] == priv.id  # закрытый чат пробуем первым
    need = [e for e in res.escalations if e.key == "need_accounts"]
    assert need and "новых аккаунтов" in need[0].title and "Public" in need[0].title
    assert need[0].severity in {"medium", "high"}


async def test_spamblock_resolves_when_spambot_clean(store: Store):
    from app.jobs import spam

    accs, chat, _ = await _seed(store)
    await store.upsert_restriction(
        accs[0].id, chat.id, "spamblock", until_at=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    )
    await store.record_setup_fail(accs[0].id, chat.id, "x", max_attempts=1)  # abandoned

    async def fake(st, acc):
        return None

    # имитация успешной проверки: SpamBot «чист»
    from app.tg import spambot

    async def fake_check(client, **kw):
        return spambot.SpamResult("clean")

    class Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    import app.jobs.spam as sp

    orig_c, orig_t = sp.check_spambot, sp.telethon_client
    sp.check_spambot, sp.telethon_client = fake_check, lambda path: Ctx()
    try:
        status = await spam.check_account_spam(store, accs[0])
    finally:
        sp.check_spambot, sp.telethon_client = orig_c, orig_t
    assert status == "clean"
    assert await store.get_restriction(accs[0].id, chat.id) is None
    st = await store.get_setup_state(accs[0].id, chat.id)
    assert st.status == "pending" and st.fail_count == 0  # пару снова можно настраивать


async def test_scan_keeps_spamblock_until_expiry_and_resolves_after(store: Store):
    from app.tg import restrictions as R

    accs, chat, _ = await _seed(store)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(timespec="seconds")
    await store.upsert_restriction(accs[0].id, chat.id, "spamblock", until_at=future)

    async def boom(*a, **k):
        raise AssertionError("участник-статус не должен снимать spamblock")

    orig = R.probe_restriction
    R.probe_restriction = boom
    try:
        res = await R.scan_account(store, object(), accs[0], [chat], {chat.id: object()})
        assert (await store.get_restriction(accs[0].id, chat.id)) is not None and res["resolved"] == 0
        await store.upsert_restriction(accs[0].id, chat.id, "spamblock", until_at=past)
        res = await R.scan_account(store, object(), accs[0], [chat], {chat.id: object()})
    finally:
        R.probe_restriction = orig
    assert res["resolved"] == 1 and res["resolved_chats"] == [chat.id]
    assert await store.get_restriction(accs[0].id, chat.id) is None


async def test_mute_expired_triggers_recheck_action(store: Store, monkeypatch):
    accs, chat, _ = await _seed(store)
    now = datetime.now(timezone.utc)
    await store.upsert_restriction(
        accs[0].id, chat.id, "mute", until_at=(now - timedelta(minutes=3)).isoformat(timespec="seconds")
    )
    await store.upsert_restriction(accs[1].id, chat.id, "ban")
    calls: list[tuple[str, int]] = []

    async def fake(name):
        async def run(st, acc_id, pks):
            calls.append((name, acc_id))
            if chat.id in pks:
                touched.append((name, acc_id))
            return True, "ok"
        return run

    touched: list[tuple[str, int]] = []
    for n in (JOIN, SETUP, RESETUP, "collect"):
        monkeypatch.setitem(engine.EXECUTORS, n, await fake(n))
    await engine.run_cycle(store, None, None, act=True)
    assert ("collect", accs[0].id) in calls           # мут закончился → перепроверить и продолжить
    assert all(acc != accs[1].id for _n, acc in touched)  # бан: с этой парой ничего не делаем


async def test_limited_account_silent_pair_becomes_spamblock_not_escalation(store: Store, monkeypatch):
    accs, chat, _ = await _seed(store)
    now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    a = accs[0]
    await store.update_account(a.id, spam_status="limited")
    await store.set_setting(f"facts_status:{a.id}", f"ok|{iso}|")
    await store.mark_scan(a.id, chat.id, "ok")
    await store.set_slot(chat.id, a.id, 5)
    await store.record_setup_ok(a.id, chat.id)
    import aiosqlite

    async with aiosqlite.connect(store.path) as db:
        await db.execute("UPDATE setup_states SET last_attempt_at=?", ((now - timedelta(hours=9)).isoformat(),))
        await db.commit()
    await store.set_setting(f"op:{RESETUP}:{a.id}:{chat.id}", f"{iso}|{now.date().isoformat()}|2")
    res = await engine.run_cycle(store, None, None, act=False)
    r = await store.get_restriction(a.id, chat.id)
    assert r is not None and r.is_spamblock and r.until_at
    assert not any("даже после пересоздания" in e.title for e in res.escalations)
