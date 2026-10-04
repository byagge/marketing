from datetime import datetime, timedelta, timezone

from app.utils.balance import (
    MUTED,
    NOT_MEMBER,
    SILENT,
    UNKNOWN,
    WORKING,
    PairFact,
    accounts_needed,
    assign_min_movement,
    circle_length,
    classify_pair,
    max_gap,
    needs_rebalance,
    plan_chat,
    positions,
    target_minutes,
)
from app.utils.minutes import even_spaced_minutes

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _fact(**kw):
    base = dict(
        account_id=1,
        chat_pk=1,
        member=1,
        can_send=1,
        sent_24h=5,
        checked_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    base.update(kw)
    return PairFact(**base)


# ---------- классификация ----------


def test_classify_working_and_unknown():
    assert classify_pair(_fact(), state_ok=True, now=NOW) == WORKING
    assert classify_pair(None, state_ok=True, now=NOW) == UNKNOWN
    assert classify_pair(_fact(member=None), state_ok=True, now=NOW) == UNKNOWN
    stale = _fact(checked_at=(NOW - timedelta(hours=40)).isoformat())
    assert classify_pair(stale, state_ok=True, now=NOW) == UNKNOWN


def test_classify_not_member_and_muted():
    assert classify_pair(_fact(member=0), state_ok=True, now=NOW) == NOT_MEMBER
    assert classify_pair(_fact(can_send=0), state_ok=True, now=NOW) == MUTED


def test_classify_silent_requires_old_setup_and_live_chat():
    old = (NOW - timedelta(hours=30)).isoformat()
    new = (NOW - timedelta(hours=2)).isoformat()
    f = _fact(sent_24h=0)
    assert classify_pair(f, state_ok=True, state_ok_since=old, now=NOW) == SILENT
    # только что настроили — ещё рано считать молчуном
    assert classify_pair(f, state_ok=True, state_ok_since=new, now=NOW) == WORKING
    # в чате молчат все → проблема чата, не аккаунта
    assert (
        classify_pair(f, state_ok=True, state_ok_since=old, now=NOW, chat_alive=False)
        == WORKING
    )
    # проба количества отправок не удалась (None) → не считаем молчуном
    assert (
        classify_pair(_fact(sent_24h=None), state_ok=True, state_ok_since=old, now=NOW)
        == WORKING
    )


# ---------- геометрия ----------


def test_gap_detects_hole():
    # 12 аккаунтов, четверть выбыла — дыра
    even = {i: m for i, m in enumerate(even_spaced_minutes(60, 12))}
    assert max_gap(positions(even, 60), 60) == 5
    assert not needs_rebalance(positions(even, 60), 60)
    holed = {a: m for a, m in even.items() if not 20 <= m <= 55}
    assert max_gap(positions(holed, 60), 60) >= 35
    assert needs_rebalance(positions(holed, 60), 60)


def test_target_minutes_interval_below_hour_avoids_collisions():
    # interval=30, 4 аккаунта: по часу было бы 0,15,30,45 → по окну 30: 0,15,0,15
    mins = target_minutes(4, 30)
    pos = sorted(m % 30 for m in mins)
    assert len(set(mins)) == 4
    assert len(set(pos)) == 4
    assert max_gap(pos, 30) == 7.5 or max_gap(pos, 30) <= 8


def test_target_minutes_hour_matches_even_spaced():
    assert target_minutes(12, 60) == even_spaced_minutes(60, 12)
    assert target_minutes(12, 360) == even_spaced_minutes(60, 12)


def test_assign_min_movement_prefers_small_shift():
    current = {1: 3, 2: 33}
    targets = [0, 30]
    got = assign_min_movement(current, [1, 2], targets, 60)
    assert got == {1: 0, 2: 30}


def test_assign_new_accounts_take_leftovers():
    got = assign_min_movement({1: 10}, [1, 2, 3], [0, 20, 40], 60)
    assert sorted(got.values()) == [0, 20, 40]
    assert got[1] == 0  # ближайший к 10


# ---------- план чата ----------


def _plan(current, statuses, **kw):
    return plan_chat(interval_minutes=kw.pop("interval", 60), current=current,
                     statuses=statuses, **kw)


def test_plan_closes_hole_after_eviction():
    # план на 12 аккаунтов, 6 выбыли (не в чате/мут) — остальные должны раздвинуться
    current = {i: m for i, m in enumerate(even_spaced_minutes(60, 12))}
    statuses = {i: (WORKING if i % 2 == 0 else NOT_MEMBER) for i in current}
    plan = _plan(current, statuses)
    assert plan.rebalanced or plan.gap_after <= plan.gap_before
    assert set(plan.evict) == {i for i in current if i % 2}
    assert len(plan.roster) == 6
    assert plan.gap_after == 10  # 60/6 — ровно
    assert len(set(plan.assign.values())) == 6


def test_plan_does_not_churn_when_even():
    current = {i: m for i, m in enumerate(even_spaced_minutes(60, 10))}
    statuses = {i: WORKING for i in current}
    plan = _plan(current, statuses)
    assert not plan.rebalanced
    assert plan.changed == set()


def test_plan_adds_working_account_without_slot_and_marks_changed():
    current = {1: 0, 2: 30}
    statuses = {1: WORKING, 2: WORKING, 3: WORKING}
    plan = _plan(current, statuses)
    assert 3 in plan.added and 3 in plan.changed
    assert len(set(plan.assign.values())) == 3


def test_plan_keeps_unknown_with_slot_but_never_adds_unknown():
    current = {1: 0, 2: 30}
    statuses = {1: WORKING, 2: UNKNOWN, 3: UNKNOWN}
    plan = _plan(current, statuses)
    assert set(plan.roster) == {1, 2}
    assert 3 not in plan.added


def test_plan_never_wipes_when_nobody_works():
    current = {1: 0, 2: 30}
    plan = _plan(current, {1: NOT_MEMBER, 2: MUTED})
    assert plan.evict == {}
    assert plan.assign == current
    assert plan.skipped_reason


def test_plan_marks_unscheduled_for_rebuild():
    current = {1: 0, 2: 30}
    plan = _plan(current, {1: WORKING, 2: WORKING}, unscheduled=[2])
    assert plan.changed == {2}


def test_long_interval_even_after_eviction():
    # 6-часовой чат, 12 аккаунтов, 6 выбыли → цикл 360, пауза ≈ 360/6 = 60
    current = {i: m for i, m in enumerate(even_spaced_minutes(60, 12))}
    statuses = {i: (WORKING if i % 2 == 0 else MUTED) for i in current}
    plan = _plan(current, statuses, interval=360)
    assert circle_length(360) == 360
    assert plan.gap_after <= 75


def test_accounts_needed():
    assert accounts_needed(5, 360) == 72
    assert accounts_needed(12, 360) == 30
    assert accounts_needed(10, 60) == 6
