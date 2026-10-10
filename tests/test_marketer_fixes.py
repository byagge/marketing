"""Разбор отчёта 10.10: ошибки Telethon, вступление по id, причины «не в чате»,
польза/вред в советнике, честный расчёт «нужно N аккаунтов»."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.context import ctx
from app.jobs import advisor
from app.models import Chat, JoinState
from app.operator import joinwhy
from app.operator.diagnose import NOT_MEMBER, PairFacts, diagnose_pair
from app.operator.needs import accounts_to_buy, accounts_to_buy_if_returning, compute_needs
from app.store import Store
from app.utils.errfmt import short_error
from app.utils.send_policy import join_due


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "fix.db")
    await db.init()
    ctx.store = db
    return db


# ---------------------------------------------------------------- ошибки Telethon

def test_short_error_hides_bytes_dump_and_explains():
    from telethon.errors import TypeNotFoundError

    e = TypeNotFoundError(0xB1B8CC83, b"\x83\xcc\xb8\xb1" * 500)
    msg = short_error(e)
    assert "telethon" in msg.lower() and "pip install -U telethon" in msg
    assert "\\x" not in msg and len(msg) < 300

    ve = ValueError("Could not find the input entity for PeerChannel(channel_id=1995593406)")
    assert "не видит этот чат" in short_error(ve)

    long = RuntimeError("x" * 1000)
    assert len(short_error(long)) <= 175


def test_telethon_is_new_enough():
    from app.tg.client import telethon_version_warning

    assert telethon_version_warning() == ""


# --------------------------------------------------------- вступление по голому id

class _NoDialogsClient:
    """Аккаунт не в чате: get_entity(int) падает, диалогов нет."""

    async def get_entity(self, target):
        raise ValueError(f"Could not find the input entity for {target}")

    async def get_input_entity(self, target):
        raise ValueError("no entity")

    async def iter_dialogs(self):
        return
        yield  # pragma: no cover

    async def __call__(self, *a, **k):
        raise AssertionError("в Telegram ходить не должны")


def _chat(**kw) -> Chat:
    base = dict(id=7, title="GSC | ЧАТ | WS Project", chat_id="-1001995593406", kind="schedule")
    base.update(kw)
    return Chat(**base)


async def test_join_by_bare_id_is_manual_not_valueerror():
    from app.tg.join import join_one

    res = await join_one(_NoDialogsClient(), _chat())
    assert res.status == "needs_manual"
    assert "ValueError" not in res.detail and "числовому id" in res.detail


async def test_membership_report_marks_bare_id_chat_as_no_link():
    from app.tg.join import check_membership

    report = await check_membership(_NoDialogsClient(), [_chat()])
    assert [c.id for c in report.no_link] == [7] and not report.missing
    linked = _chat(id=8, username="gsc_chat")
    report = await check_membership(_NoDialogsClient(), [linked])
    assert [c.id for c in report.missing] == [8]


# ------------------------------------------------------- ссылка у аккаунта в чате

async def test_harvest_takes_username_then_exported_invite():
    from telethon.tl import types

    from app.tg.linkharvest import harvest_join_link, is_no_link_error

    pub = SimpleNamespace(username="gsc_chat", usernames=None)
    assert (await harvest_join_link(_NoDialogsClient(), pub)).username == "gsc_chat"

    class Client:
        async def __call__(self, req):
            return SimpleNamespace(
                full_chat=SimpleNamespace(exported_invite=SimpleNamespace(link="https://t.me/+abc"))
            )

    chan = types.Channel(
        id=1, title="t", photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc), megagroup=True
    )
    link = await harvest_join_link(Client(), chan)
    assert link.invite == "https://t.me/+abc" and link.describe() == "invite-ссылку"

    class Denied:
        async def __call__(self, req):
            raise PermissionError("admin required")

    assert not (await harvest_join_link(Denied(), chan)).found
    assert is_no_link_error("нет способа вступления") and not is_no_link_error("FloodWait")


async def test_new_link_wakes_manual_joins(store: Store):
    a = await store.add_account("x")
    chat = await store.add_chat("C", "-1001995593406", kind="schedule")
    await store.record_join_result(a.id, chat.id, status="manual", error="нет способа вступления")
    assert await store.revive_chat_joins(chat.id) == 1
    st = await store.get_join_state(a.id, chat.id)
    assert st.status == "pending" and st.fail_count == 0


# -------------------------------------------------------- причины «не в чате»

def _state(**kw) -> JoinState:
    base = dict(id=1, account_id=1, chat_pk=7)
    base.update(kw)
    return JoinState(**base)


def test_explain_not_member_reasons():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    pub = _chat(username="gsc_chat")
    inv = _chat(invite_link="https://t.me/+abc")

    assert joinwhy.explain_not_member(pub, None, banned=True, ban_detail="UserBanned", now=now).code == "ban"
    assert joinwhy.explain_not_member(_chat(), None, now=now).code == "no_link"
    assert joinwhy.explain_not_member(pub, _state(status="requested", last_attempt_at=now.isoformat()), now=now).code == "request"
    assert joinwhy.explain_not_member(pub, _state(status="manual", last_error="бот"), now=now).code == "manual"
    # спамблок мешает только публичным чатам; закрытый (invite) — вступаем
    assert joinwhy.explain_not_member(pub, None, spam_limited=True, now=now).code == "spamblock"
    assert joinwhy.explain_not_member(inv, None, spam_limited=True, now=now).code == "ready"
    gave = _state(status="abandoned", fail_count=5, last_error="FloodWait", last_attempt_at=now.isoformat())
    assert joinwhy.explain_not_member(pub, gave, now=now).code == "gave_up"
    wait = _state(fail_count=1, last_error="x", last_attempt_at=now.isoformat())
    assert joinwhy.explain_not_member(pub, wait, now=now).code == "wait"


def test_abandoned_join_is_retried_later_not_forever():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    old = _state(status="abandoned", fail_count=5, last_attempt_at=(now - timedelta(hours=80)).isoformat())
    fresh = _state(status="abandoned", fail_count=5, last_attempt_at=(now - timedelta(hours=10)).isoformat())
    assert not join_due(old, now=now)  # старое поведение без параметра
    assert join_due(old, now=now, abandoned_retry_hours=72)
    assert not join_due(fresh, now=now, abandoned_retry_hours=72)


# ------------------------------------------------------ расчёт «нужно N аккаунтов»

def _pf(**kw) -> PairFacts:
    base = dict(
        account_id=1, account_label="a", chat_pk=1, chat_title="Услуги S&A", interval=60,
        has_slot=True, setup_status="ok", setup_age_min=600, scan_status="ok",
        scan_age_min=20, sends_3h=1,
    )
    base.update(kw)
    return PairFacts(**base)


def test_need_does_not_count_accounts_that_cannot_join():
    diag = []
    for i in range(4):  # 4 работают
        f = _pf(account_id=i, account_label=f"w{i}")
        diag.append((f, diagnose_pair(f)))
    for i, code in enumerate(("ready", "spamblock", "gave_up", "no_link"), start=10):
        f = _pf(account_id=i, account_label=f"n{i}", scan_status="not_member",
                has_join_method=code != "no_link", join_code=code, sends_3h=0)
        diag.append((f, diagnose_pair(f)))
    n = compute_needs(diag, [])[0]
    # «вступит сама» верно только для ready; остальные трое застряли
    assert n.needed == 12 and n.eligible == 5 and n.stuck == 3
    # у застрявших по спамблоку / «сдался» оператор не дублирует вступление
    assert diagnose_pair(diag[5][0]).action is None and diagnose_pair(diag[4][0]).action == "join"
    assert diagnose_pair(diag[4][0]).cause == NOT_MEMBER


def test_need_is_max_deficit_not_sum_and_counts_returning_mutes():
    diag = []
    # чат A (интервал 365 → нужно 73), чат B (интервал 60 → 12); те же 6 аккаунтов в обоих
    for chat_pk, interval in ((1, 365), (2, 60)):
        for i in range(6):
            f = _pf(account_id=i, account_label=f"a{i}", chat_pk=chat_pk, interval=interval,
                    chat_title=f"chat{chat_pk}")
            diag.append((f, diagnose_pair(f)))
        for i in range(10, 14):  # 4 замьючены на известный срок
            f = _pf(account_id=i, account_label=f"m{i}", chat_pk=chat_pk, interval=interval,
                    chat_title=f"chat{chat_pk}", restriction="mute", restriction_until="2026-10-10 18:00")
            diag.append((f, diagnose_pair(f)))
    needs = compute_needs(diag, [])
    # interval 365 is capped to 60 min: both chats need 12, not 73 (the 5-min target is
    # unreachable for a slow chat and must not inflate the purchase number)
    assert {n.title: n.needed for n in needs} == {"chat1": 12, "chat2": 12}
    assert accounts_to_buy(needs) == 12 - 6
    assert accounts_to_buy_if_returning(needs) == 12 - 6 - 4
    assert {n.title: n.returning for n in needs} == {"chat1": 4, "chat2": 4}


# ---------------------------------------------------- советник: польза против вреда

def test_score_and_levels():
    v, h = advisor.score_account(leads_7d=0, sent_48h=0, working=0, bans=2, soft_blocks=0, strikes=0, limited=True)
    assert (v, h) == (0.0, 7.0)
    kw = dict(min_harm=4.0, pending_share=0.0, pending_human=False, warn_share=0.35)
    assert advisor.decide_level(value=v, harm=h, leads_7d=0, limited=True, handled=False, **kw) == "stop"
    # те же баны, но аккаунт приносит клиентов — не «отключить»
    v2, h2 = advisor.score_account(leads_7d=4, sent_48h=40, working=5, bans=2, soft_blocks=0, strikes=0, limited=True)
    assert advisor.decide_level(value=v2, harm=h2, leads_7d=4, limited=True, handled=False, **kw) != "stop"
    # спамблок взят под контроль (sender выключен): не ругаемся, а информируем
    v3, h3 = advisor.score_account(leads_7d=0, sent_48h=30, working=6, bans=0, soft_blocks=0, strikes=0, limited=True)
    assert advisor.decide_level(value=v3, harm=h3, leads_7d=0, limited=True, handled=True, **kw) == "info"


async def _acc(store: Store, label: str, **fields):
    a = await store.add_account(label)
    await store.update_account(a.id, telethon_session=f"/tmp/{label}.session", **fields)
    return await store.get_account(a.id)


async def test_spamblock_with_sender_goes_dead_and_keeps_working(store: Store):
    limited = await _acc(store, "faraon", spam_status="limited", sender_account_id="s-1")
    plain = await _acc(store, "nosender", spam_status="limited")  # sender нет — выключать нечего
    clean = await _acc(store, "clean", sender_account_id="s-2")
    assert advisor.will_demote(limited) and not advisor.will_demote(plain) and not advisor.will_demote(clean)
    assert await advisor.demote_sender_on_spam(store, limited)
    again = await store.get_account(limited.id)
    assert again.is_dead and not await advisor.demote_sender_on_spam(store, again)
    assert not (await store.get_account(clean.id)).is_dead

    chat = await store.add_chat("Чат", "-1001000000001", kind="schedule")
    await store.set_slot(chat.id, limited.id, 5)
    await store.record_setup_ok(limited.id, chat.id)
    adv = {a.account.label: a for a in await advisor.build_advice(store, demoted={limited.id})}
    f = adv["faraon"]
    assert f.level == "info" and "dead" in f.text()
    assert "больше вреда" not in f.text()  # работает в чате, поэтому «отключить» не советуем


async def test_advice_explains_why_not_in_chat(store: Store):
    acc = await _acc(store, "ton", spam_status="limited")
    pub = await store.add_chat("Публичный", "-1001000000002", kind="schedule", username="pubchat")
    closed = await store.add_chat("Закрытый", "-1001000000003", kind="schedule", invite_link="https://t.me/+x")
    nolink = await store.add_chat("Без ссылки", "-1001000000004", kind="schedule")
    banned = await store.add_chat("Бан", "-1001000000005", kind="schedule", username="banchat")
    for c in (pub, closed, nolink, banned):
        await store.mark_scan(acc.id, c.id, "not_member")
    await store.record_ban(acc.id, banned.id, "join_ban", "UserBannedInChannel")
    f = {a.account.label: a for a in await advisor.build_advice(store)}["ton"]
    text = f.text()
    codes = {n: c for n, c, _w in f.pending_in}
    assert codes == {"Публичный": "spamblock", "Закрытый": "ready", "Без ссылки": "no_link"}
    assert any("Бан" in b and "бан" in b for b in f.blocked_in)
    assert "вступлю сама, когда снимется спамблок" in text and "ЖДУ ССЫЛКУ" in text
    assert "не вступлен/недоступен" not in text
