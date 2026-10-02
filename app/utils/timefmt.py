from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings


def parse_utc(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def fmt_local(raw: str | datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    dt = parse_utc(raw) if isinstance(raw, str) else raw
    if dt is None:
        return "—"
    return dt.astimezone(get_settings().tz).strftime(fmt)


def fmt_until(raw: str) -> str:
    """Срок мута: дата или «бессрочно / неизвестно»."""
    dt = parse_utc(raw)
    if dt is None:
        return "бессрочно/неизв."
    return fmt_local(dt, "%d.%m.%Y %H:%M")
