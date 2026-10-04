from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

COVER_HOURS = 23
MAX_SCHEDULED = 99


def resolve_repeat_period(is_premium: bool, configured: int | None) -> int | None:
    """
    schedule_repeat_period is Premium-only (Telegram 403 otherwise).
    Non-Premium accounts get a one-shot grid; the bot re-schedules daily.
    """
    if not is_premium:
        return None
    if configured is None:
        return None
    period = int(configured)
    return period if period > 0 else None


def posts_count_for_interval(interval_minutes: int) -> int:
    minutes = max(1, int(interval_minutes))
    needed = max(1, math.ceil((COVER_HOURS * 60) / minutes))
    return min(needed, MAX_SCHEDULED)


def dead_interval(chat_interval: int, dead_setting: int) -> int:
    """Интервал для dead-аккаунта: чаще, но не плотнее, чем покрывают 99 слотов на сутки."""
    floor = math.ceil((COVER_HOURS * 60) / MAX_SCHEDULED)
    return max(floor, min(int(chat_interval), int(dead_setting)))


def phase_offset_minutes(rank: int, interval_minutes: int) -> int:
    """
    Для редких интервалов (>60 мин) разнести аккаунты чата по «фазам» по часам.

    Без сдвига все аккаунты стартуют в 09:mm и пишут пачкой в одном часу, потом
    interval-60 минут тишины (при 6 ч — 5 часов). rank — позиция аккаунта среди
    слотов чата по минуте; соседние по минуте уходят в разные фазы.
    """
    interval = int(interval_minutes)
    if interval <= 60:
        return 0
    phases = interval // 60
    return (max(0, int(rank)) % phases) * 60


def build_schedule_times(
    start_minute: int,
    interval_minutes: int,
    posts_count: int | None = None,
    *,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
    start_hour: int = 9,
    offset_minutes: int = 0,
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
    if offset_minutes:
        base += timedelta(minutes=int(offset_minutes))
    times: list[datetime] = []
    for i in range(count):
        dt = base + timedelta(minutes=interval * i + i)
        if dt <= threshold:
            dt += timedelta(days=1)
        times.append(dt)
    times.sort()
    return times
