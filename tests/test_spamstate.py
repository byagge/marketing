from datetime import datetime, timedelta, timezone

import pytest

from app.models import SpamState
from app.utils.spamstate import (
    check_due,
    compute_load,
    evaluate_check,
    parse_spambot_reply,
    parse_until,
    scale_parallel,
    scale_seconds,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


CLEAN_TEXTS = [
    "Good news, no limits are currently applied to your account. You're free as a bird!",
    "Ваш аккаунт свободен от каких-либо ограничений.",
    "Хорошие новости, ограничений нет!",
]
LIMITED_TEXTS = [
    "Unfortunately, some phone numbers may be limited. Your account is limited until 7 Oct 2026, 14:30 UTC",
    "I'm very sorry that you had to contact me. Unfortunately, your account was limited.",
    "К сожалению, ваш аккаунт ограничен. Ограничения будут автоматически сняты 7 окт 2026, 14:30",
    "Мне очень жаль. Ваш аккаунт ограничен до 9 января 2027",
    "Your account was blocked for violations of the Telegram Terms of Service",
]


@pytest.mark.parametrize("text", CLEAN_TEXTS)
def test_clean_replies(text):
    st = parse_spambot_reply(text)
    assert st.known and not st.limited


@pytest.mark.parametrize("text", LIMITED_TEXTS)
def test_limited_replies(text):
    st = parse_spambot_reply(text)
    assert st.known and st.limited


@pytest.mark.parametrize("text", ["", "hello", "Please wait", "/start"])
def test_unknown_reply_is_not_a_verdict(text):
    st = parse_spambot_reply(text)
    assert not st.known and not st.limited


def test_until_parsing():
    assert parse_until("limited until 7 Oct 2026, 14:30 UTC") == datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)
    assert parse_until("сняты 7 окт 2026, 14:30") == datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)
    assert parse_until("до 9 января 2027") == datetime(2027, 1, 9, tzinfo=timezone.utc)
    assert parse_until("до 9 мая 2027") == datetime(2027, 5, 9, tzinfo=timezone.utc)
    assert parse_until("до 31 фев 2027") is None  # несуществующая дата не роняет разбор
    assert parse_until("no date here") is None


def test_load_ladder_during_spamblock():
    st = SpamState(account_id=1, status="limited", limited_since=iso(NOW))
    kw = dict(ramp_1=6, ramp_2=24)
    assert compute_load(st, now=NOW, **kw) == 50
    assert compute_load(st, now=NOW + timedelta(hours=5.9), **kw) == 50
    assert compute_load(st, now=NOW + timedelta(hours=6), **kw) == 25
    assert compute_load(st, now=NOW + timedelta(hours=23.9), **kw) == 25
    assert compute_load(st, now=NOW + timedelta(hours=24), **kw) == 0
    assert compute_load(st, now=NOW + timedelta(days=5), **kw) == 0


def test_load_dead_and_recovery():
    st = SpamState(account_id=1)
    assert compute_load(st, now=NOW) == 100
    assert compute_load(st, dead=True, now=NOW) == 0
    st = SpamState(account_id=1, status="clean", cleared_at=iso(NOW))
    assert compute_load(st, now=NOW + timedelta(hours=1), recovery=6) == 50
    assert compute_load(st, now=NOW + timedelta(hours=6), recovery=6) == 100
    # dead важнее всего
    lim = SpamState(account_id=1, status="limited", limited_since=iso(NOW))
    assert compute_load(lim, dead=True, now=NOW) == 0


def test_scaling():
    assert scale_seconds(60, 100) == 60
    assert scale_seconds(60, 50) == 120
    assert scale_seconds(60, 25) == 240
    assert scale_seconds(60, 0) == 6000  # защита от деления на 0: крайне редко
    assert scale_parallel(4, 100) == 4
    assert scale_parallel(4, 50) == 2
    assert scale_parallel(4, 25) == 1
    assert scale_parallel(1, 25) == 1


def _limited():
    return parse_spambot_reply(LIMITED_TEXTS[0])


def _clean():
    return parse_spambot_reply(CLEAN_TEXTS[0])


def test_first_spamblock_is_strike_one_and_not_dead():
    out = evaluate_check(SpamState(account_id=1), _limited(), now=NOW, dead_strikes=3)
    assert out.state.status == "limited" and out.state.strikes == 1
    assert out.state.limited_since == iso(NOW)
    assert out.events == ["limited_new"] and not out.make_dead
    assert out.state.limited_until.startswith("2026-10-07T14:30")


def test_continuing_spamblock_does_not_add_strikes():
    st = evaluate_check(SpamState(account_id=1), _limited(), now=NOW).state
    out = evaluate_check(st, _limited(), now=NOW + timedelta(hours=3))
    assert out.state.strikes == 1
    assert out.state.limited_since == st.limited_since  # ступени считаются от первого обнаружения
    assert out.events == ["limited_again"]


def test_clear_then_repeat_reaches_dead_on_third():
    st = SpamState(account_id=1)
    t = NOW
    for expected in (1, 2):
        st = evaluate_check(st, _limited(), now=t, dead_strikes=3).state
        assert st.strikes == expected
        t += timedelta(hours=10)
        out = evaluate_check(st, _clean(), now=t)
        assert out.events == ["cleared"] and out.state.status == "clean"
        assert out.state.limited_since == "" and out.state.cleared_at == iso(t)
        st = out.state
        t += timedelta(days=1)
    out = evaluate_check(st, _limited(), now=t, dead_strikes=3)
    assert out.state.strikes == 3 and out.make_dead and "dead" in out.events


def test_dead_strikes_threshold_is_configurable():
    st = SpamState(account_id=1, strikes=3)
    st.status = "clean"
    out = evaluate_check(st, _limited(), now=NOW, dead_strikes=4)
    assert out.state.strikes == 4 and out.make_dead
    st2 = SpamState(account_id=1, strikes=2)
    assert not evaluate_check(st2, _limited(), now=NOW, dead_strikes=4).make_dead


def test_strikes_decay_after_long_clean_period():
    st = SpamState(account_id=1, strikes=2, status="clean", cleared_at=iso(NOW - timedelta(days=31)))
    out = evaluate_check(st, _clean(), now=NOW, decay_days=30)
    assert out.state.strikes == 0 and "strikes_decayed" in out.events
    fresh = SpamState(account_id=1, strikes=2, status="clean", cleared_at=iso(NOW - timedelta(days=5)))
    assert evaluate_check(fresh, _clean(), now=NOW, decay_days=30).state.strikes == 2


def test_unknown_reply_changes_nothing():
    st = SpamState(account_id=1, status="limited", strikes=2, limited_since=iso(NOW))
    out = evaluate_check(st, parse_spambot_reply("???"), now=NOW + timedelta(hours=1))
    assert out.state == st and out.events == [] and not out.make_dead


def test_check_due():
    assert check_due(SpamState(account_id=1), now=NOW)
    st = SpamState(account_id=1, last_check_at=iso(NOW - timedelta(hours=2)))
    assert not check_due(st, now=NOW, every_hours=3)
    assert check_due(st, now=NOW, every_hours=1)
