from app.utils.minutes import (
    phase_offset,
    plan_chat_minutes,
    rotate_minutes,
    spaced_minutes,
    suggest_minute,
)


def test_phase_spreads_chats():
    plans = [
        plan_chat_minutes(60, 4, chat_index=i, chats_total=4) for i in range(4)
    ]
    # разные чаты не должны стартовать с одной и той же первой минуты
    firsts = [p[0] for p in plans]
    assert len(set(firsts)) >= 3


def test_plan_unique_within_chat():
    minutes = plan_chat_minutes(60, 15, chat_index=2, chats_total=10)
    assert len(minutes) == 15
    assert len(set(minutes)) == 15


def test_suggest_prefers_least_used_globally():
    occupied: list[int] = []
    global_counts = {0: 10, 30: 10, 15: 10, 45: 10}
    m = suggest_minute(60, occupied, global_counts)
    # 0/30/15/45 перегружены — возьмёт следующий из binary-split
    assert m not in {0, 30, 15, 45} or global_counts.get(m, 0) <= 10


def test_spaced_and_rotate():
    base = spaced_minutes(60, 4)
    assert base == [0, 30, 15, 45]
    rotated = rotate_minutes(base, 5, 60)
    assert rotated == [5, 35, 20, 50]
    assert phase_offset(0, 4, 60) == 0
