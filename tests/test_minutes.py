from app.utils.minutes import (
    TABLE_PERIOD,
    TableFullError,
    binary_split_sequence,
    even_spaced_minutes,
    min_gap_stats,
    period_for_interval,
    plan_chat_minutes,
    suggest_minute,
)


def test_period_always_hour():
    assert period_for_interval(15) == 60
    assert period_for_interval(30) == 60
    assert period_for_interval(60) == 60
    assert period_for_interval(120) == 60


def test_even_four():
    assert even_spaced_minutes(60, 4) == [0, 15, 30, 45]


def test_even_fifteen_equal_gaps():
    mins = even_spaced_minutes(60, 15)
    assert len(mins) == 15
    assert len(set(mins)) == 15
    stats = min_gap_stats(mins)
    # все промежутки = 4
    assert stats["min_gap"] == 4
    assert stats["max_gap"] == 4


def test_hour_first_adds_spread():
    occupied: list[int] = []
    got = []
    for _ in range(4):
        m = suggest_minute(60, occupied)
        got.append(m)
        occupied.append(m)
    # равные окна: 0, затем ~30, затем ~15/45
    assert got[0] == 0
    assert sorted(got) == [0, 15, 30, 45]


def test_no_cluster_like_interval_15():
    """Раньше period=15 давал 0..14 подряд — больше нельзя."""
    occupied: list[int] = []
    got = []
    for _ in range(15):
        m = suggest_minute(15, occupied)  # даже если передали 15 — час
        got.append(m)
        occupied.append(m)
    assert max(got) >= 45  # растянуто по часу
    assert sorted(got) == even_spaced_minutes(60, 15) or min_gap_stats(got)["min_gap"] >= 2


def test_sequence_binary_compat():
    seq = binary_split_sequence(60)
    assert seq[:8] == [0, 30, 15, 45, 7, 22, 37, 52]


def test_table_full():
    occupied = list(range(60))
    try:
        suggest_minute(60, occupied)
        raise AssertionError("expected TableFullError")
    except TableFullError:
        pass


def test_plan_unique_and_phased():
    plans = [
        plan_chat_minutes(60, 4, chat_index=i, chats_total=4) for i in range(4)
    ]
    firsts = [p[0] for p in plans]
    assert len(set(firsts)) >= 3
    for p in plans:
        assert len(set(p)) == 4
        st = min_gap_stats(p)
        assert st["min_gap"] == st["max_gap"] == 15


def test_table_period_constant():
    assert TABLE_PERIOD == 60
