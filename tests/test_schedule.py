from datetime import datetime
from zoneinfo import ZoneInfo

from app.utils.schedule import build_schedule_times, posts_count_for_interval

TZ = ZoneInfo("Asia/Bishkek")


def test_hour_grid_drift():
    now = datetime(2026, 9, 8, 8, 0, tzinfo=TZ)
    times = build_schedule_times(0, 60, posts_count=4, now=now, tz=TZ, start_hour=9)
    assert [t.strftime("%H:%M") for t in times] == ["09:00", "10:01", "11:02", "12:03"]


def test_past_slots_move_to_tomorrow():
    now = datetime(2026, 9, 8, 12, 0, tzinfo=TZ)
    times = build_schedule_times(0, 60, posts_count=3, now=now, tz=TZ, start_hour=9)
    assert times[0].day == 9
    assert times[0].strftime("%H:%M") == "09:00"


def test_posts_count_hour():
    assert posts_count_for_interval(60) == 23


def test_half_hour_count():
    assert posts_count_for_interval(30) == 46
