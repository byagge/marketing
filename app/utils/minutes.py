"""Равномерное заполнение минутных слотов (двоичное деление периода).

Пустая таблица → :00, следующий :30, затем :15, :45, потом середины
оставшихся промежутков. Существующие минуты не двигаются.
"""

from __future__ import annotations

from collections.abc import Iterable


class TableFullError(Exception):
    def __init__(self, period: int) -> None:
        super().__init__(f"Все {period} минутных слотов заняты")
        self.period = period


def period_for_interval(interval_minutes: int) -> int:
    minutes = max(1, int(interval_minutes))
    return min(minutes, 60)


def binary_split_sequence(period: int) -> list[int]:
    if period <= 0:
        raise ValueError("period must be > 0")

    seen: list[int] = []
    seen_set: set[int] = set()

    def add(minute: int) -> None:
        minute %= period
        if minute not in seen_set:
            seen_set.add(minute)
            seen.append(minute)

    add(0)
    step = period
    while step > 1:
        half = step // 2
        if half <= 0:
            break
        for start in range(0, period, step):
            add(start + half)
        step = half

    for minute in range(period):
        add(minute)
    return seen


def suggest_minute(period: int, occupied: Iterable[int]) -> int:
    occ = {int(x) % period for x in occupied}
    for minute in binary_split_sequence(period):
        if minute not in occ:
            return minute
    raise TableFullError(period)


def format_minute(minute: int) -> str:
    return f"{int(minute) % 60:02d}"
