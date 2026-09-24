"""Равномерное заполнение минутных слотов в пределах часа (0–59).

Важно: interval чата задаёт частоту постов, но таблица минут всегда
на весь час. Иначе при interval=15 все аккаунты сжимаются в :00–:14.

Внутри чата — равные промежутки (0, 15, 30, 45 …).
Между чатами — фазовый сдвиг, чтобы не бить в одну минуту.
При точечном добавлении — минута в самом большом «окне».
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

TABLE_PERIOD = 60


class TableFullError(Exception):
    def __init__(self, period: int = TABLE_PERIOD) -> None:
        super().__init__(f"Все {period} минутных слотов заняты")
        self.period = period


def period_for_interval(interval_minutes: int | None = None) -> int:
    """Размер таблицы минут — всегда час. interval на размер не влияет."""
    del interval_minutes
    return TABLE_PERIOD


def binary_split_sequence(period: int = TABLE_PERIOD) -> list[int]:
    """Исторический порядок (для тестов / совместимости)."""
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


def even_spaced_minutes(
    period: int,
    count: int,
    *,
    offset: int = 0,
) -> list[int]:
    """
    Равномерно по кругу часа: для N=4 → 0,15,30,45; для N=15 → каждые 4 мин.
    ``offset`` — фазовый сдвиг чата относительно других.
    """
    period = max(1, int(period))
    n = max(0, min(int(count), period))
    if n == 0:
        return []
    offset = int(offset) % period
    used: set[int] = set()
    out: list[int] = []
    for i in range(n):
        ideal = (i * period) // n
        m = (ideal + offset) % period
        if m in used:
            for delta in range(1, period):
                cand = (m + delta) % period
                if cand not in used:
                    m = cand
                    break
        used.add(m)
        out.append(m)
    return out


def spaced_minutes(period: int, count: int) -> list[int]:
    """Алиас: равномерные минуты без сдвига."""
    return even_spaced_minutes(period, count, offset=0)


def phase_offset(chat_index: int, chats_total: int, period: int = TABLE_PERIOD) -> int:
    """Сдвиг фазы, чтобы соседние чаты не занимали одни и те же минуты."""
    period = max(1, int(period))
    if chats_total <= 1 or period <= 1:
        return 0
    return (int(chat_index) * period) // int(chats_total)


def rotate_minutes(minutes: Sequence[int], offset: int, period: int) -> list[int]:
    period = max(1, int(period))
    offset = int(offset) % period
    out: list[int] = []
    used: set[int] = set()
    for raw in minutes:
        m = (int(raw) + offset) % period
        if m in used:
            for delta in range(1, period):
                cand = (m + delta) % period
                if cand not in used:
                    m = cand
                    break
        used.add(m)
        out.append(m)
    return out


def _circular_min_dist(minute: int, occupied: Sequence[int], period: int) -> int:
    if not occupied:
        return period
    best = period
    for o in occupied:
        d = min((minute - o) % period, (o - minute) % period)
        if d < best:
            best = d
    return best


def suggest_minute(
    period: int | None = None,
    occupied: Iterable[int] | None = None,
    global_counts: dict[int, int] | None = None,
) -> int:
    """
    Свободная минута в самом большом окне (max min-distance по кругу).
    При равенстве — наименее загруженная глобально.
    """
    period = TABLE_PERIOD if period is None else max(1, int(period))
    # если передали старый period=15 — всё равно работаем в часе
    if period < TABLE_PERIOD:
        period = TABLE_PERIOD
    occ = sorted({int(x) % period for x in (occupied or [])})
    free = [m for m in range(period) if m not in occ]
    if not free:
        raise TableFullError(period)

    def key(m: int) -> tuple:
        # больше зазор → лучше; меньше глобальная нагрузка → лучше
        gap = _circular_min_dist(m, occ, period)
        load = int((global_counts or {}).get(m, 0))
        return (-gap, load, m)

    return min(free, key=key)


def plan_chat_minutes(
    period: int | None,
    account_count: int,
    *,
    chat_index: int = 0,
    chats_total: int = 1,
) -> list[int]:
    """План минут для одного чата: равные промежутки + фаза относительно других."""
    period = TABLE_PERIOD if not period or period < TABLE_PERIOD else int(period)
    offset = phase_offset(chat_index, chats_total, period)
    return even_spaced_minutes(period, account_count, offset=offset)


def format_minute(minute: int) -> str:
    return f"{int(minute) % 60:02d}"


def min_gap_stats(minutes: Sequence[int], period: int = TABLE_PERIOD) -> dict[str, float]:
    """Диагностика равномерности (для тестов/логов)."""
    period = max(1, int(period))
    vals = sorted({int(m) % period for m in minutes})
    if len(vals) <= 1:
        return {"count": float(len(vals)), "min_gap": float(period), "max_gap": float(period)}
    gaps = [(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
    gaps.append(vals[0] + period - vals[-1])
    return {
        "count": float(len(vals)),
        "min_gap": float(min(gaps)),
        "max_gap": float(max(gaps)),
        "ideal_gap": float(period) / len(vals),
    }
