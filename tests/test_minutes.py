from app.utils.minutes import TableFullError, binary_split_sequence, period_for_interval, suggest_minute


def test_hour_first_four():
    occupied: list[int] = []
    got = []
    for _ in range(4):
        m = suggest_minute(60, occupied)
        got.append(m)
        occupied.append(m)
    assert got == [0, 30, 15, 45]


def test_sequence_starts_with_midpoints():
    seq = binary_split_sequence(60)
    assert seq[:8] == [0, 30, 15, 45, 7, 22, 37, 52]


def test_does_not_move_existing():
    occupied = [0, 30]
    assert suggest_minute(60, occupied) == 15
    occupied.append(15)
    assert suggest_minute(60, occupied) == 45


def test_period_half_hour():
    assert period_for_interval(30) == 30
    occupied = []
    got = [suggest_minute(30, occupied)]
    occupied.append(got[-1])
    got.append(suggest_minute(30, occupied))
    assert got == [0, 15]


def test_table_full():
    occupied = list(range(60))
    try:
        suggest_minute(60, occupied)
        raise AssertionError("expected TableFullError")
    except TableFullError:
        pass
