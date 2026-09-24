from app.utils.minutes import (
    even_spaced_minutes,
    min_gap_stats,
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
    firsts = [p[0] for p in plans]
    assert len(set(firsts)) >= 3


def test_plan_unique_within_chat():
    minutes = plan_chat_minutes(60, 15, chat_index=2, chats_total=10)
    assert len(minutes) == 15
    assert len(set(minutes)) == 15
    st = min_gap_stats(minutes)
    assert st["min_gap"] == st["max_gap"]


def test_suggest_prefers_largest_gap_and_low_global():
    occupied = [0, 30]
    # середины окон: 15 и 45 одинаково далеки; global давит на 15
    global_counts = {15: 10, 45: 0}
    m = suggest_minute(60, occupied, global_counts)
    assert m == 45


def test_spaced_and_rotate():
    base = spaced_minutes(60, 4)
    assert base == [0, 15, 30, 45]
    rotated = rotate_minutes(base, 5, 60)
    assert rotated == [5, 20, 35, 50]
    assert phase_offset(0, 4, 60) == 0
    assert phase_offset(1, 4, 60) == 15


def test_even_not_clustered_at_start():
    mins = even_spaced_minutes(60, 12)
    # не должно быть 0,1,2,...,11
    assert mins != list(range(12))
    assert max(mins) - min(mins) >= 50 or min_gap_stats(mins)["min_gap"] >= 4
