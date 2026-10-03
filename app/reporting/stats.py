"""Оценка фактических отправок schedule / sender."""

from __future__ import annotations

from datetime import datetime, timezone


def expected_per_chat_24h(interval_minutes: int) -> float:
    """Сколько постов должен уйти в один schedule-чат за 24ч при стабильной сетке."""
    minutes = max(1, int(interval_minutes))
    return 24.0 * 60.0 / minutes


def estimate_schedule_sent(
    *,
    prev_count: int | None,
    curr_count: int,
    expected: int,
    is_premium: bool,
    interval_minutes: int,
    hours_elapsed: float,
) -> float:
    """
    Оценка отправленных между двумя снимками очереди.

    Non-premium: сообщения уходят из очереди → delta = max(0, prev - curr).
    Premium (daily repeat): очередь почти не уменьшается → оценка по интервалу.
    """
    hours = max(0.0, float(hours_elapsed))
    if hours <= 0:
        return 0.0

    if is_premium:
        return min(expected_per_chat_24h(interval_minutes) * (hours / 24.0), float(expected) * 2)

    if prev_count is None:
        # Первый снимок: если очередь полная — ещё почти ничего не ушло;
        # если ниже expected — разницу считаем уже отправленной с момента setup.
        if curr_count >= max(1, expected):
            return 0.0
        return float(max(0, expected - curr_count))

    dropped = max(0, int(prev_count) - int(curr_count))
    # Если cron перезалил сетку, count мог вырасти — тогда dropped=0.
    return float(dropped)


def parse_iso(ts: str) -> datetime | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def hours_between(earlier: str, later: str | None = None) -> float:
    a = parse_iso(earlier)
    if not a:
        return 0.0
    b = parse_iso(later) if later else datetime.now(timezone.utc)
    if not b:
        b = datetime.now(timezone.utc)
    return max(0.0, (b - a).total_seconds() / 3600.0)


def local_day(tz_name: str = "Asia/Bishkek") -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz_name)).date().isoformat()
