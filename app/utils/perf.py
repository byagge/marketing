"""Результативность аккаунта: вердикт по числу написавших и жизненный цикл флага «переоформить»."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from app.models import AccountPerf

VERDICT_TEXT = {
    "none": "никто не пишет",
    "low": "пишут мало",
    "ok": "нормально",
    "grace": "переоформлен, ждём результат",
    "": "не измерялся",
}


def evaluate_verdict(wrote_n: int, *, low_threshold: int = 3) -> str:
    """none — не написал никто, low — меньше порога, ok — достаточно."""
    if wrote_n <= 0:
        return "none"
    if wrote_n < max(1, int(low_threshold)):
        return "low"
    return "ok"


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def days_since(ts: str, now: datetime) -> float | None:
    dt = _parse(ts)
    return None if dt is None else (now - dt).total_seconds() / 86400


def hours_since(ts: str, now: datetime) -> float | None:
    days = days_since(ts, now)
    return None if days is None else days * 24


def in_grace(perf: AccountPerf, now: datetime, grace_days: int) -> bool:
    d = days_since(perf.redesigned_at, now)
    return d is not None and d < grace_days


def check_due(perf: AccountPerf, now: datetime, every_hours: float) -> bool:
    h = hours_since(perf.checked_at, now)
    return h is None or h >= every_hours


@dataclass
class FlagOutcome:
    perf: AccountPerf
    event: str = ""  # flagged | reminder | recovered | ""


def apply_measurement(
    prev: AccountPerf,
    *,
    new_n: int,
    wrote_n: int,
    cloak_n: int,
    scanned: int,
    truncated: bool,
    window_days: int,
    now: datetime,
    low_threshold: int = 3,
    grace_days: int = 7,
    renotify_days: int = 7,
) -> FlagOutcome:
    """Свести новое измерение с прошлым состоянием: вердикт, флаг, событие для уведомления."""
    stamp = _iso(now)
    cur = replace(
        prev,
        checked_at=stamp,
        window_days=window_days,
        new_n=new_n,
        wrote_n=wrote_n,
        cloak_n=cloak_n,
        scanned=scanned,
        truncated=int(truncated),
    )
    if in_grace(prev, now, grace_days):
        cur.verdict = "grace"
        return FlagOutcome(cur)
    cur.verdict = evaluate_verdict(wrote_n, low_threshold=low_threshold)
    if cur.verdict in {"none", "low"}:
        if not prev.flagged_at:
            cur.flagged_at, cur.notified_at = stamp, stamp
            return FlagOutcome(cur, "flagged")
        waited = days_since(prev.notified_at, now)
        if waited is None or waited >= renotify_days:
            cur.notified_at = stamp
            return FlagOutcome(cur, "reminder")
        return FlagOutcome(cur)
    if prev.flagged_at:
        cur.flagged_at = cur.notified_at = ""
        return FlagOutcome(cur, "recovered")
    return FlagOutcome(cur)


def mark_redesigned(prev: AccountPerf, now: datetime) -> AccountPerf:
    return replace(
        prev, flagged_at="", notified_at="", redesigned_at=_iso(now), verdict="grace"
    )


def age_days(created_at: str, now: datetime) -> float:
    d = days_since(created_at, now)
    return 10_000.0 if d is None else d  # нет даты создания — считаем давним


def minus_days(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)
