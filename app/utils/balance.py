"""Перераспределение минутных слотов по фактически работающим аккаунтам.

Проблема: сетка минут раздаётся всем аккаунтам × всем чатам, но часть пар не
работает (не в чате, мут, молчат). Их минуты — «дыры»: вместо пауз 5–8 мин
получаем 40+. Здесь — чистая логика (без Telegram и БД): кто в строю,
где дыры, как раздвинуть оставшихся равномерно и с минимальными сдвигами.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.utils.minutes import (
    TABLE_PERIOD,
    TableFullError,
    even_spaced_minutes,
    phase_offset,
    suggest_minute,
)
from app.utils.schedule import phase_offset_minutes

# Статусы пары «аккаунт × чат»
WORKING = "working"
UNKNOWN = "unknown"  # факта нет/устарел/проба упала → не трогаем (ни добавить, ни выгнать)
NOT_MEMBER = "not_member"
MUTED = "muted"
SILENT = "silent"  # настроено давно, но за сутки ни одной отправки


def circle_length(interval_minutes: int) -> int:
    """Длина круга, по которому считаем равномерность."""
    return max(1, int(interval_minutes))


def positions(minutes: Mapping[int, int], interval_minutes: int) -> list[int]:
    """
    Положение каждого слота на круге цикла чата (отсортировано).

    interval ≤ 60: минута mod interval (аккаунт пишет раз в interval мин, значит
    важна минута внутри этого окна, а не внутри часа).
    interval > 60: фаза по часам (как в schedule_one_chat: ранг по минуте % k)
    + минута.
    """
    interval = max(1, int(interval_minutes))
    ordered = sorted(minutes.items(), key=lambda kv: (int(kv[1]), int(kv[0])))
    out: list[int] = []
    for rank, (_aid, minute) in enumerate(ordered):
        if interval > TABLE_PERIOD:
            out.append((phase_offset_minutes(rank, interval) + int(minute)) % interval)
        else:
            out.append(int(minute) % interval)
    return sorted(out)


def max_gap(pos: Sequence[int], circle: int) -> float:
    """Максимальная пауза между соседними отправками по кругу (мин)."""
    circle = max(1, int(circle))
    vals = sorted({int(p) % circle for p in pos})
    if not vals:
        return float(circle)
    if len(vals) == 1:
        return float(circle)
    gaps = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    gaps.append(vals[0] + circle - vals[-1])
    return float(max(gaps))


def ideal_gap(count: int, circle: int) -> float:
    return float(circle) / max(1, int(count))


def needs_rebalance(
    pos: Sequence[int], circle: int, *, factor: float = 1.5, slack: float = 2.0
) -> bool:
    """Есть заметный перекос: макс. пауза заметно больше идеальной."""
    n = len({int(p) % max(1, circle) for p in pos})
    if n <= 1:
        return False
    ideal = ideal_gap(n, circle)
    return max_gap(pos, circle) > max(ideal * factor, ideal + slack)


def target_minutes(
    count: int, interval_minutes: int, *, offset: int = 0
) -> list[int]:
    """
    Минуты (0–59) для count аккаунтов так, чтобы положения были равномерны.

    interval ≥ 60 → равномерно по часу (фазы по часам дадут равномерность по циклу).
    interval < 60 → равномерно внутри окна interval: иначе минуты 0,15,30,45 при
    interval=30 попадают на одни и те же 0/15 — «две кампании в одну минуту».
    """
    n = max(0, min(int(count), TABLE_PERIOD))
    if n == 0:
        return []
    interval = max(1, int(interval_minutes))
    if interval >= TABLE_PERIOD:
        return even_spaced_minutes(TABLE_PERIOD, n, offset=offset)
    used: set[int] = set()
    seen: dict[int, int] = {}
    out: list[int] = []
    for j in range(n):
        p = ((j * interval) // n + offset) % interval
        t = seen.get(p, 0)
        seen[p] = t + 1
        m = p + interval * t
        while m >= TABLE_PERIOD or m in used:
            m = (m + 1) % TABLE_PERIOD
        used.add(m)
        out.append(m)
    return out


def _circ_dist(a: int, b: int, circle: int) -> int:
    d = (a - b) % circle
    return min(d, circle - d)


def assign_min_movement(
    current: Mapping[int, int],
    accounts: Sequence[int],
    targets: Sequence[int],
    interval_minutes: int,
) -> dict[int, int]:
    """
    Раздать targets аккаунтам с минимальным суммарным сдвигом.

    Существующие слоты сохраняют порядок по кругу и вращаются целиком;
    новые аккаунты (без слота) получают оставшиеся минуты.
    """
    if len(targets) < len(accounts):
        raise ValueError("targets меньше аккаунтов")
    circle = min(max(1, int(interval_minutes)), TABLE_PERIOD)
    tgt = sorted(targets, key=lambda m: (m % circle, m))
    n = len(tgt)
    existing = sorted(
        (a for a in accounts if a in current),
        key=lambda a: (int(current[a]) % circle, int(current[a]), a),
    )
    fresh = [a for a in accounts if a not in current]
    e = len(existing)
    result: dict[int, int] = {}
    used_idx: set[int] = set()
    if e:
        best_r, best_cost = 0, None
        for r in range(n):
            idx = [(r + (i * n) // e) % n for i in range(e)]
            cost = sum(
                _circ_dist(int(current[a]) % circle, tgt[ix] % circle, circle)
                for a, ix in zip(existing, idx)
            )
            if best_cost is None or cost < best_cost:
                best_cost, best_r = cost, r
        for i, a in enumerate(existing):
            ix = (best_r + (i * n) // e) % n
            result[a] = tgt[ix]
            used_idx.add(ix)
    rest = [tgt[i] for i in range(n) if i not in used_idx]
    for a, m in zip(fresh, rest):
        result[a] = m
    return result


# ---------- статусы пар ----------


@dataclass
class PairFact:
    account_id: int
    chat_pk: int
    member: int | None = None  # 1/0/None(не знаем)
    can_send: int | None = None
    mute_until: str = ""
    sent_24h: int | None = None
    last_sent_at: str = ""
    error: str = ""
    checked_at: str = ""
    # сколько scheduled-сообщений реально стоит у аккаунта в этом чате (None = не проверяли)
    scheduled_count: int | None = None


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify_pair(
    fact: PairFact | None,
    *,
    state_ok: bool,
    state_ok_since: str = "",
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=30),
    silent_after: timedelta = timedelta(hours=26),
    chat_alive: bool = True,
) -> str:
    """Работает ли пара на самом деле (по факту, а не по плану)."""
    now = now or datetime.now(timezone.utc)
    if fact is None or fact.member is None:
        return UNKNOWN
    checked = _parse_iso(fact.checked_at)
    if checked is None or now - checked > max_age:
        return UNKNOWN
    if not fact.member:
        return NOT_MEMBER
    if fact.can_send is not None and not fact.can_send:
        return MUTED
    if state_ok and fact.sent_24h == 0 and chat_alive:
        since = _parse_iso(state_ok_since)
        if since is not None and now - since > silent_after:
            return SILENT
    return WORKING


# ---------- план по чату ----------


@dataclass
class ChatPlan:
    roster: list[int] = field(default_factory=list)
    evict: dict[int, str] = field(default_factory=dict)  # account_id → причина
    added: list[int] = field(default_factory=list)
    assign: dict[int, int] = field(default_factory=dict)  # итоговые минуты roster
    changed: set[int] = field(default_factory=set)  # кого пересобрать в Telegram
    rebalanced: bool = False
    gap_before: float = 0.0  # реальная пауза с учётом только живых слотов
    gap_after: float = 0.0
    nominal_gap: float = 0.0  # пауза «по плану» (все назначенные слоты)
    skipped_reason: str = ""


def plan_chat(
    *,
    interval_minutes: int,
    current: Mapping[int, int],
    statuses: Mapping[int, str],
    unscheduled: Iterable[int] = (),
    chat_index: int = 0,
    chats_total: int = 1,
    factor: float = 1.5,
) -> ChatPlan:
    """
    current — account_id → минута; statuses — account_id → WORKING/UNKNOWN/...
    unscheduled — аккаунты, у которых в Telegram ничего не настроено (нужна сборка).
    """
    interval = max(1, int(interval_minutes))
    circle = circle_length(interval)
    plan = ChatPlan()
    plan.nominal_gap = max_gap(positions(current, interval), circle)

    working = [a for a, s in statuses.items() if s == WORKING]
    unknown_with_slot = [a for a, s in statuses.items() if s == UNKNOWN and a in current]
    roster = list(dict.fromkeys(working + unknown_with_slot))
    plan.roster = roster

    alive_now = {a: m for a, m in current.items() if a in set(roster)}
    plan.gap_before = max_gap(positions(alive_now, interval), circle) if alive_now else float(circle)

    if not roster:
        plan.skipped_reason = "нет работающих аккаунтов — слоты не трогаю"
        plan.assign = dict(current)
        plan.gap_after = plan.nominal_gap
        return plan

    for a in current:
        if a not in set(roster):
            plan.evict[a] = statuses.get(a, "нет в ростере")

    keep = dict(alive_now)
    occupied = list(keep.values())
    for a in roster:
        if a in keep:
            continue
        try:
            m = suggest_minute(TABLE_PERIOD, occupied)
        except TableFullError:
            continue
        keep[a] = m
        occupied.append(m)
        plan.added.append(a)

    roster = [a for a in roster if a in keep]
    plan.roster = roster
    pos = positions(keep, interval)
    if needs_rebalance(pos, circle, factor=factor):
        count = len(roster)
        offset = phase_offset(chat_index, chats_total, min(circle, TABLE_PERIOD))
        targets = target_minutes(count, interval, offset=offset)
        assign = assign_min_movement(
            {a: keep[a] for a in roster if a in alive_now}, roster, targets, interval
        )
        plan.rebalanced = True
    else:
        assign = {a: keep[a] for a in roster}
    plan.assign = assign
    plan.gap_after = max_gap(positions(assign, interval), circle)

    before_phase = _phase_map(current, interval)
    after_phase = _phase_map(assign, interval)
    unsched = set(unscheduled)
    for a in roster:
        if (
            a in plan.added
            or assign[a] != current.get(a)
            or before_phase.get(a) != after_phase.get(a)
            or a in unsched
        ):
            plan.changed.add(a)
    return plan


def _phase_map(minutes: Mapping[int, int], interval: int) -> dict[int, int]:
    if interval <= TABLE_PERIOD:
        return {a: 0 for a in minutes}
    ordered = sorted(minutes.items(), key=lambda kv: (int(kv[1]), int(kv[0])))
    return {
        a: phase_offset_minutes(rank, interval) for rank, (a, _m) in enumerate(ordered)
    }


def expected_sends_per_hour(count: int, interval_minutes: int) -> float:
    """Сколько сообщений в чат за час даёт count аккаунтов при данном интервале."""
    return count * 60.0 / max(1, int(interval_minutes))


def accounts_needed(target_gap_minutes: float, interval_minutes: int) -> int:
    """Сколько аккаунтов нужно, чтобы пауза в чате была не больше target_gap."""
    return max(1, math.ceil(max(1, int(interval_minutes)) / max(0.5, target_gap_minutes)))
