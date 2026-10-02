from datetime import datetime
from zoneinfo import ZoneInfo

from app.utils.schedule import build_schedule_times, posts_count_for_interval, resolve_repeat_period

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


def test_resolve_repeat_period_premium_only():
    assert resolve_repeat_period(True, 86400) == 86400
    assert resolve_repeat_period(True, 0) is None
    assert resolve_repeat_period(True, None) is None
    assert resolve_repeat_period(False, 86400) is None
    assert resolve_repeat_period(False, 0) is None


def test_nonpremium_hour_defaults_before_start():
    from app.config import Settings

    s = Settings.model_construct(start_hour=9, nonpremium_reschedule_hour=None)
    assert s.nonpremium_hour == 8
    s2 = Settings.model_construct(start_hour=0, nonpremium_reschedule_hour=None)
    assert s2.nonpremium_hour == 23
    s3 = Settings.model_construct(start_hour=9, nonpremium_reschedule_hour=7)
    assert s3.nonpremium_hour == 7


def test_rolling_grid_starts_now_not_at_start_hour():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.utils.schedule import build_schedule_times, rolling_posts_count

    tz = ZoneInfo("Asia/Bishkek")
    now = datetime(2026, 10, 2, 11, 14, tzinfo=tz)  # поздняя пересборка
    times = build_schedule_times(5, 60, now=now, tz=tz, start_hour=9, rolling=True)
    assert len(times) == rolling_posts_count(60)
    assert times[0] == datetime(2026, 10, 2, 12, 5, tzinfo=tz)
    assert all(b > a for a, b in zip(times, times[1:]))
    # покрытие ≥ 24 ч — следующая суточная пересборка не оставляет дыр
    assert (times[-1] - times[0]).total_seconds() >= 24 * 3600


def test_fixed_grid_loses_morning_slots_when_job_is_late():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.utils.schedule import build_schedule_times

    tz = ZoneInfo("Asia/Bishkek")
    now = datetime(2026, 10, 2, 11, 14, tzinfo=tz)
    times = build_schedule_times(5, 60, now=now, tz=tz, start_hour=9)
    # слоты 09:05 и 10:06 уехали на завтра — сегодня они потеряны
    assert sum(1 for t in times if t.day == 3 and t.hour in (9, 10)) >= 2
