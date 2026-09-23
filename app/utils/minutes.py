"""Равномерное заполнение минутных слотов.

Внутри чата — двоичное деление периода (0 → 30 → 15 → 45 → …).
Между чатами — фазовый сдвиг + учёт глобальной плотности, чтобы
в overview не было «пустых» колонок в начале/конце часа.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


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


def spaced_minutes(period: int, count: int) -> list[int]:
    """Первые ``count`` минут из binary-split (равномерно внутри периода)."""
    period = max(1, int(period))
    n = max(0, int(count))
    if n == 0:
        return []
    seq = binary_split_sequence(period)
    return seq[: min(n, period)]


def phase_offset(chat_index: int, chats_total: int, period: int) -> int:
    """Сдвиг фазы, чтобы соседние чаты не занимали одни и те же минуты."""
    if chats_total <= 1 or period <= 1:
        return 0
    return int(round(chat_index * period / chats_total)) % period


def rotate_minutes(minutes: Sequence[int], offset: int, period: int) -> list[int]:
    period = max(1, int(period))
    offset = int(offset) % period
    out: list[int] = []
    used: set[int] = set()
    for raw in minutes:
        m = (int(raw) + offset) % period
        if m in used:
            # коллизия после сдвига — ищем ближайшую свободную
            for delta in range(1, period):
                cand = (m + delta) % period
                if cand not in used:
                    m = cand
                    break
        used.add(m)
        out.append(m)
    return out


def suggest_minute(
    period: int,
    occupied: Iterable[int],
    global_counts: dict[int, int] | None = None,
) -> int:
    """Свободная минута: binary-split, при наличии global_counts — наименее загруженная."""
    occ = {int(x) % period for x in occupied}
    candidates = [m for m in binary_split_sequence(period) if m not in occ]
    if not candidates:
        raise TableFullError(period)
    if not global_counts:
        return candidates[0]
    order = {m: i for i, m in enumerate(candidates)}
    return min(
        candidates,
        key=lambda m: (int(global_counts.get(m, 0)), order[m]),
    )


def plan_chat_minutes(
    period: int,
    account_count: int,
    *,
    chat_index: int = 0,
    chats_total: int = 1,
) -> list[int]:
    """План минут для одного чата с фазовым сдвигом относительно других."""
    base = spaced_minutes(period, account_count)
    offset = phase_offset(chat_index, chats_total, period)
    return rotate_minutes(base, offset, period)


def format_minute(minute: int) -> str:
    return f"{int(minute) % 60:02d}"
