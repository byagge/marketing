"""Отчёт по ФАКТАМ: только то, что реально подтверждено историей чатов и диалогов.

Никаких оценок «по плану» и «по очереди»: отправка = сообщение аккаунта, которое
есть в истории чата (собирается раз в час). Если данных не хватает — так и пишем.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median

from app.config import get_settings
from app.models import Account, Chat
from app.store import Store
from app.utils.facts import day_start, window_utc
from app.utils.timefmt import fmt_local, parse_utc, to_iso


@dataclass
class ChatFact:
    chat_pk: int
    title: str
    priority: bool
    sends_1h: int = 0
    sends_3h: int = 0
    sends_day: int = 0
    senders_day: int = 0
    senders_3h: int = 0
    working_planned: int = 0  # аккаунтов с рабочим слотом (план)
    avg_gap: float | None = None  # реальная средняя пауза за окно, мин
    median_gap: float | None = None
    max_gap: int | None = None
    last_send_at: str = ""
    banned: int = 0
    muted: int = 0
    interval: int = 60


@dataclass
class AccountFact:
    account_id: int
    label: str
    sends_day: int = 0
    chats_day: int = 0
    planned_chats: int = 0
    spam_status: str = ""
    spam_until: str = ""
    spam_checked_at: str = ""
    banned: int = 0
    muted: int = 0
    dm_wrote: int = 0  # людей написали аккаунту (всего)
    dm_first_total: int = 0  # из них написали первыми
    dm_first_day: int = 0  # новых за выбранные сутки
    dm_active_day: int = 0  # писали в выбранные сутки
    dm_first_24h: int = 0
    dm_first_7d: int = 0
    dm_active_24h: int = 0
    dm_waiting: int = 0
    dm_pending: int = 0
    facts_status: str = ""  # ok | err | ""
    facts_at: str = ""
    facts_err: str = ""


@dataclass
class FactReport:
    day: str
    is_today: bool
    window_from: datetime
    window_to: datetime
    covered_from: datetime | None
    last_collect: datetime | None
    accounts_total: int
    accounts_fresh: int
    failed: list[str] = field(default_factory=list)
    total_day: int = 0
    total_prev_day: int = 0
    chats: list[ChatFact] = field(default_factory=list)
    accounts: list[AccountFact] = field(default_factory=list)
    dm_pending_total: int = 0

    @property
    def data_note(self) -> str:
        notes: list[str] = []
        if self.covered_from and self.covered_from > self.window_from + timedelta(minutes=30):
            notes.append(f"данные только с {fmt_local(self.covered_from, '%d.%m %H:%M')}")
        if self.last_collect is None:
            notes.append("сбор ещё не запускался")
        elif self.is_today:
            age = (datetime.now(timezone.utc) - self.last_collect).total_seconds() / 60
            if age > 90:
                notes.append(f"последний сбор {age:.0f} мин назад")
        if self.failed:
            notes.append(f"не прочитано аккаунтов: {len(self.failed)}")
        return "; ".join(notes)


def gap_stats(
    times: list[datetime], start: datetime, end: datetime
) -> tuple[float | None, float | None, int | None]:
    """(средняя, медианная, максимальная пауза) в минутах между отправками в окне."""
    span = (end - start).total_seconds() / 60
    if span <= 0:
        return None, None, None
    pts = sorted(t for t in times if start <= t <= end)
    if not pts:
        return None, None, int(span)
    avg = span / len(pts)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(pts, pts[1:])]
    edges = [(pts[0] - start).total_seconds() / 60, (end - pts[-1]).total_seconds() / 60]
    med = median(gaps) if gaps else None
    return avg, med, int(max(gaps + edges))


async def build_fact_report(store: Store, day: str | None = None) -> FactReport:
    s = get_settings()
    tz = s.tz
    now = datetime.now(timezone.utc)
    today_local = now.astimezone(tz).date().isoformat()
    day = day or today_local
    d0 = datetime.fromisoformat(day).replace(tzinfo=tz)
    day_from = d0
    day_to = d0 + timedelta(days=1)
    is_today = day == today_local
    window_to = min(now, day_to)

    events = await store.send_events_between(*window_utc(day_from, 24 * 60))
    prev_events = await store.send_events_between(
        *window_utc(day_from - timedelta(days=1), 24 * 60)
    )
    # границы реально собранных данных
    oldest = parse_utc(await store.first_send_at())
    scans = await store.list_scans()
    last_collect = max((parse_utc(sc[2]) for sc in scans if parse_utc(sc[2])), default=None)

    window_from = day_from
    if oldest is not None and oldest > window_from:
        window_from_eff = oldest
    else:
        window_from_eff = window_from

    chats = [c for c in await store.list_chats(enabled_only=True) if c.kind in {"schedule", "sender"}]
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    restr = await store.list_restrictions()
    active = await store.active_slots()
    from app.ui.facts_view import is_priority

    chat_ev: dict[int, list[tuple[int, datetime]]] = {}
    acc_ev: dict[int, list[tuple[int, datetime]]] = {}
    for a, c, t in events:
        dt = parse_utc(t)
        if dt is None:
            continue
        chat_ev.setdefault(c, []).append((a, dt))
        acc_ev.setdefault(a, []).append((c, dt))

    ban_by_chat: dict[int, int] = {}
    mute_by_chat: dict[int, int] = {}
    ban_by_acc: dict[int, int] = {}
    mute_by_acc: dict[int, int] = {}
    restr_keys = set()
    for r in restr:
        restr_keys.add((r.account_id, r.chat_pk))
        if r.is_ban:
            ban_by_chat[r.chat_pk] = ban_by_chat.get(r.chat_pk, 0) + 1
            ban_by_acc[r.account_id] = ban_by_acc.get(r.account_id, 0) + 1
        else:
            mute_by_chat[r.chat_pk] = mute_by_chat.get(r.chat_pk, 0) + 1
            mute_by_acc[r.account_id] = mute_by_acc.get(r.account_id, 0) + 1
    planned_by_chat: dict[int, int] = {}
    planned_by_acc: dict[int, int] = {}
    for sl in active:
        if (sl.account_id, sl.chat_pk) in restr_keys:
            continue
        planned_by_chat[sl.chat_pk] = planned_by_chat.get(sl.chat_pk, 0) + 1
        planned_by_acc[sl.account_id] = planned_by_acc.get(sl.account_id, 0) + 1

    h1 = now - timedelta(hours=1)
    h3 = now - timedelta(hours=3)
    chat_rows: list[ChatFact] = []
    for chat in chats:
        evs = chat_ev.get(chat.id, [])
        times = [t for _a, t in evs]
        avg, med, mx = gap_stats(times, window_from_eff, window_to)
        cf = ChatFact(
            chat_pk=chat.id,
            title=chat.display_name,
            priority=is_priority(chat),
            sends_day=len(evs),
            senders_day=len({a for a, _t in evs}),
            sends_1h=sum(1 for _a, t in evs if t >= h1),
            sends_3h=sum(1 for _a, t in evs if t >= h3),
            senders_3h=len({a for a, t in evs if t >= h3}),
            working_planned=planned_by_chat.get(chat.id, 0),
            avg_gap=avg,
            median_gap=med,
            max_gap=mx,
            last_send_at=to_iso(max(times)) if times else "",
            banned=ban_by_chat.get(chat.id, 0),
            muted=mute_by_chat.get(chat.id, 0),
            interval=chat.interval_minutes,
        )
        chat_rows.append(cf)
    chat_rows.sort(key=lambda c: (not c.priority, -c.sends_day, c.title.casefold()))

    # люди в ЛС
    since_24h = to_iso(now - timedelta(hours=24))
    since_7d = to_iso(now - timedelta(days=7))
    summary = await store.dm_summary(since_24h, since_7d)
    active_day = await store.dm_active_on_day(day)
    new_day = await store.dm_new_on_day(*window_utc(day_from, 24 * 60))

    fresh = 0
    failed: list[str] = []
    acc_rows: list[AccountFact] = []
    dm_pending_total = 0
    for acc in accounts:
        evs = acc_ev.get(acc.id, [])
        status_raw = await store.get_setting(f"facts_status:{acc.id}", "")
        st, _, rest = status_raw.partition("|")
        at, _, err = rest.partition("|")
        at_dt = parse_utc(at)
        if st == "ok" and at_dt and (now - at_dt) < timedelta(minutes=150):
            fresh += 1
        elif st == "err":
            failed.append(acc.label)
        pend = int(await store.get_setting(f"dm_pending:{acc.id}", "0") or 0)
        dm_pending_total += pend
        d = summary.get(acc.id, {})
        acc_rows.append(
            AccountFact(
                account_id=acc.id,
                label=acc.label,
                sends_day=len(evs),
                chats_day=len({c for c, _t in evs}),
                planned_chats=planned_by_acc.get(acc.id, 0),
                spam_status=acc.spam_status,
                spam_until=acc.spam_until,
                spam_checked_at=acc.spam_checked_at,
                banned=ban_by_acc.get(acc.id, 0),
                muted=mute_by_acc.get(acc.id, 0),
                dm_wrote=d.get("wrote", 0),
                dm_first_total=d.get("first_total", 0),
                dm_first_day=new_day.get(acc.id, 0),
                dm_active_day=active_day.get(acc.id, 0),
                dm_first_24h=d.get("first_24h", 0),
                dm_first_7d=d.get("first_7d", 0),
                dm_active_24h=d.get("active_24h", 0),
                dm_waiting=d.get("waiting", 0),
                dm_pending=pend,
                facts_status=st,
                facts_at=at,
                facts_err=err,
            )
        )
    acc_rows.sort(key=lambda a: (-a.dm_first_day, -a.sends_day, a.label.casefold()))

    return FactReport(
        day=day,
        is_today=is_today,
        window_from=window_from,
        window_to=window_to,
        covered_from=oldest,
        last_collect=last_collect,
        accounts_total=len(accounts),
        accounts_fresh=fresh,
        failed=failed,
        total_day=len(events),
        total_prev_day=len(prev_events),
        chats=chat_rows,
        accounts=acc_rows,
        dm_pending_total=dm_pending_total,
    )
