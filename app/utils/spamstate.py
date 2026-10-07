"""Спамблок: разбор ответа @SpamBot, ступени нагрузки, счётчик повторов и dead-режим."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from app.models import SpamState

LOAD_FULL = 100


@dataclass(frozen=True)
class SpamStatus:
    """Результат разбора ответа @SpamBot."""

    known: bool  # False — ответ не распознан, состояние менять нельзя
    limited: bool = False
    until: datetime | None = None
    text: str = ""


_CLEAN_MARKERS = (
    "no limits are currently applied",
    "good news",
    "free as a bird",
    "свободен от каких-либо ограничений",
    "нет ограничений",
    "ограничений не",
    "хорошие новости",
)
_LIMITED_MARKERS = (
    "unfortunately",
    "is limited",
    "was limited",
    "are limited",
    "account is now limited",
    "limited until",
    "restricted",
    "terms of service",
    "к сожалению",
    "сожалею",
    "мне очень жаль",
    "заблокирован",
)
# «ограничен/ограничена/ограничены», но не «ограничений/ограничения» (это есть и в «чисто»)
_LIMITED_RU_RE = re.compile(r"ограничен(?:а|о|ы)?(?![а-яё])")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}  # fmt: skip

_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-zА-Яа-яЁё]{3,10})\.?,?\s+(\d{4})(?:\D{1,6}?(\d{1,2}):(\d{2}))?"
)


def parse_until(text: str) -> datetime | None:
    """Дата снятия ограничений из текста бота (UTC), если она там есть."""
    for m in _DATE_RE.finditer(text or ""):
        month = _MONTHS.get(m.group(2)[:3].casefold())
        if not month:
            continue
        try:
            return datetime(
                int(m.group(3)),
                month,
                int(m.group(1)),
                int(m.group(4) or 0),
                int(m.group(5) or 0),
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
    return None


def parse_spambot_reply(text: str) -> SpamStatus:
    raw = text or ""
    low = raw.casefold()
    limited = any(m in low for m in _LIMITED_MARKERS) or bool(_LIMITED_RU_RE.search(low))
    clean = any(m in low for m in _CLEAN_MARKERS)
    if limited:
        return SpamStatus(known=True, limited=True, until=parse_until(raw), text=raw)
    if clean:
        return SpamStatus(known=True, limited=False, text=raw)
    return SpamStatus(known=False, text=raw)


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _hours_since(ts: str, now: datetime) -> float | None:
    dt = _parse_ts(ts)
    return None if dt is None else (now - dt).total_seconds() / 3600


def compute_load(
    state: SpamState,
    *,
    dead: bool = False,
    now: datetime | None = None,
    ramp_1: float = 6.0,
    ramp_2: float = 24.0,
    recovery: float = 6.0,
) -> int:
    """
    Нагрузка sender в процентах: 100 / 50 / 25 / 0.

    dead → 0. Спамблок: 50% сразу, 25% после ramp_1 ч, 0% (стоп sender) после ramp_2 ч.
    После снятия: recovery часов на 50%, потом 100%.
    """
    if dead:
        return 0
    moment = now or datetime.now(timezone.utc)
    if state.is_limited:
        hours = _hours_since(state.limited_since, moment)
        if hours is None or hours < ramp_1:
            return 50
        if hours < ramp_2:
            return 25
        return 0
    if state.cleared_at:
        hours = _hours_since(state.cleared_at, moment)
        if hours is not None and hours < recovery:
            return 50
    return LOAD_FULL


def scale_seconds(value: int, load: int) -> int:
    """Растянуть интервал под нагрузку: при 50% — вдвое реже. load<=0 — не вызывать."""
    load = max(1, min(LOAD_FULL, int(load)))
    return int(round(int(value) * LOAD_FULL / load))


def scale_parallel(value: int, load: int) -> int:
    return max(1, int(int(value) * max(0, min(LOAD_FULL, int(load))) / LOAD_FULL))


@dataclass
class CheckOutcome:
    state: SpamState
    events: list[str]  # limited_new | limited_again | cleared | dead | strikes_decayed
    make_dead: bool = False


def evaluate_check(
    state: SpamState,
    status: SpamStatus,
    *,
    now: datetime | None = None,
    dead_strikes: int = 3,
    decay_days: int = 30,
) -> CheckOutcome:
    """Свести новый ответ @SpamBot с сохранённым состоянием. Не мутирует state."""
    moment = now or datetime.now(timezone.utc)
    stamp = _iso(moment)
    if not status.known:
        return CheckOutcome(state=state, events=[])
    new = replace(state, last_check_at=stamp, last_text=status.text[:500])
    events: list[str] = []
    if status.limited:
        new.limited_until = _iso(status.until) if status.until else ""
        if not state.is_limited:
            new.status = "limited"
            new.limited_since = stamp
            new.cleared_at = ""
            new.strikes = state.strikes + 1
            events.append("limited_new")
        else:
            events.append("limited_again")
        make_dead = new.strikes >= max(1, int(dead_strikes))
        if make_dead:
            events.append("dead")
        return CheckOutcome(state=new, events=events, make_dead=make_dead)
    # чисто
    if state.is_limited:
        new.status = "clean"
        new.limited_since = ""
        new.limited_until = ""
        new.cleared_at = stamp
        events.append("cleared")
    elif state.strikes and decay_days > 0:
        hours = _hours_since(state.cleared_at, moment)
        if hours is not None and hours >= decay_days * 24:
            new.strikes = 0
            events.append("strikes_decayed")
    return CheckOutcome(state=new, events=events)


def check_due(state: SpamState, *, now: datetime | None = None, every_hours: float = 3.0) -> bool:
    moment = now or datetime.now(timezone.utc)
    hours = _hours_since(state.last_check_at, moment)
    return hours is None or hours >= every_hours


def plus_hours(ts: str, hours: float) -> str:
    dt = _parse_ts(ts)
    return _iso(dt + timedelta(hours=hours)) if dt else ""
