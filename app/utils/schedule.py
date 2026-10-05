from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DAY_MINUTES = 24 * 60
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
    """
    Сколько слотов нужно, чтобы сетка закрывала РОВНО сутки.

    Слот = interval + 1 мин дрейфа. Раньше брали 23 часа — для 60 мин это 23 слота
    по 61 мин = 23,4 ч, и каждые сутки у всех аккаунтов чата оставалась общая дыра
    ≈40 мин (около 08:20–09:00, на репите Premium и после суточной пересборки).
    """
    step = max(1, int(interval_minutes)) + 1
    needed = max(1, round(DAY_MINUTES / step))
    return min(needed, MAX_SCHEDULED)


ROLLING_HOURS = 26


def rolling_posts_count(interval_minutes: int) -> int:
    """Сколько слотов нужно, чтобы покрыть ~26 ч (с запасом до следующей пересборки)."""
    step = max(1, int(interval_minutes)) + 1
    return max(1, min(MAX_SCHEDULED, math.ceil(ROLLING_HOURS * 60 / step)))


def dead_interval(chat_interval: int, dead_setting: int) -> int:
    """Интервал для dead-аккаунта: чаще, но не плотнее, чем покрывают 99 слотов на сутки."""
    floor = math.ceil(DAY_MINUTES / MAX_SCHEDULED) - 1
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
    rolling: bool = False,
    offset_minutes: int = 0,
) -> list[datetime]:
    """Сетка как в source/every_hour.py: 09:mm, затем каждый слот +interval и +1 мин дрейфа.

    rolling=True (аккаунты без Premium, где нет schedule_repeat): сетка стартует
    с ближайшего слота «сейчас», а не с start_hour. Тогда суточная пересборка может
    идти в любое время и не теряет слоты до своего окончания (раньше при долгом
    прогоне утренние слоты уезжали на завтра).
    """
    if tz is None:
        tz = ZoneInfo("Asia/Bishkek")
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    minute = max(0, min(59, int(start_minute)))
    interval = max(1, int(interval_minutes))
    threshold = now + timedelta(seconds=30)

    if rolling:
        count = posts_count if posts_count is not None else rolling_posts_count(interval)
        first = now.replace(minute=minute, second=0, microsecond=0)
        while first <= threshold:
            first += timedelta(hours=1)
        return [first + timedelta(minutes=interval * i + i) for i in range(count)]

    count = posts_count if posts_count is not None else posts_count_for_interval(interval_minutes)

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
