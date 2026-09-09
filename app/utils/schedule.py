from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

COVER_HOURS = 23
MAX_SCHEDULED = 99


def posts_count_for_interval(interval_minutes: int) -> int:
    minutes = max(1, int(interval_minutes))
    needed = max(1, math.ceil((COVER_HOURS * 60) / minutes))
    return min(needed, MAX_SCHEDULED)


def build_schedule_times(
    start_minute: int,
    interval_minutes: int,
    posts_count: int | None = None,
    *,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
    start_hour: int = 9,
) -> list[datetime]:
    """Сетка как в source/every_hour.py: 09:mm, затем каждый слот +interval и +1 мин дрейфа."""
    if tz is None:
        tz = ZoneInfo("Asia/Bishkek")
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    count = posts_count if posts_count is not None else posts_count_for_interval(interval_minutes)
    minute = max(0, min(59, int(start_minute)))
    interval = max(1, int(interval_minutes))
    threshold = now + timedelta(seconds=30)

    base = now.replace(hour=start_hour, minute=minute, second=0, microsecond=0)
    times: list[datetime] = []
    for i in range(count):
        dt = base + timedelta(minutes=interval * i + i)
        if dt <= threshold:
            dt += timedelta(days=1)
        times.append(dt)
    times.sort()
    return times
