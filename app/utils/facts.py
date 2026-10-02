"""Чистые функции для таблиц фактов отправки (без Telegram и БД)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.utils.timefmt import parse_utc

Event = tuple[int, int, str]  # account_id, chat_pk, sent_at (UTC iso)


def window_utc(start_local: datetime, minutes: int) -> tuple[str, str]:
    start = start_local.astimezone(timezone.utc)
    end = start + timedelta(minutes=minutes)
    return (
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
    )


def last_hour_window(now: datetime, tz: ZoneInfo) -> datetime:
    """Начало окна «последние 60 минут» (по целым минутам)."""
    now = now.astimezone(tz).replace(second=0, microsecond=0)
    return now - timedelta(minutes=59)


def hour_window(day: datetime, hour: int, tz: ZoneInfo) -> datetime:
    local = day.astimezone(tz)
    return local.replace(hour=hour, minute=0, second=0, microsecond=0)


def day_start(now: datetime, tz: ZoneInfo, days_ago: int = 0) -> datetime:
    local = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return local - timedelta(days=days_ago)


def minute_cells(
    events: list[Event],
    start_local: datetime,
    minutes: int,
    tz: ZoneInfo,
) -> dict[tuple[int, int], list[int]]:
    """(номер строки, chat_pk) → [account_id, …] отправивших в эту минуту."""
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    base = start_local.astimezone(tz).replace(second=0, microsecond=0)
    for account_id, chat_pk, sent_at in events:
        dt = parse_utc(sent_at)
        if dt is None:
            continue
        local = dt.astimezone(tz).replace(second=0, microsecond=0)
        idx = int((local - base).total_seconds() // 60)
        if 0 <= idx < minutes:
            cells[(idx, chat_pk)].append(account_id)
    return cells


def hour_counts(
    events: list[Event], day_start_local: datetime, tz: ZoneInfo
) -> dict[tuple[int, int], int]:
    """(час 0–23, chat_pk) → число отправок за сутки, начиная с day_start_local."""
    counts: dict[tuple[int, int], int] = defaultdict(int)
    base = day_start_local.astimezone(tz)
    for _account_id, chat_pk, sent_at in events:
        dt = parse_utc(sent_at)
        if dt is None:
            continue
        local = dt.astimezone(tz)
        idx = int((local - base).total_seconds() // 3600)
        if 0 <= idx < 24:
            counts[(idx, chat_pk)] += 1
    return counts


def expected_per_hour(working_accounts: int, interval_minutes: int) -> float:
    """Сколько отправок в час ждём от чата при N рабочих аккаунтах."""
    step = max(1, int(interval_minutes)) + 1  # +1 мин дрейфа сетки
    return working_accounts * 60.0 / step
