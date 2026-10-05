"""Экраны «Факты отправки»: час / сутки по данным истории чатов."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from app.config import get_settings
from app.jobs.coverage import build_coverage
from app.models import Chat
from app.store import Store
from app.ui.emoji import pe
from app.ui.table_image import try_grid_png
from app.utils.facts import (
    day_start,
    expected_per_hour,
    hour_counts,
    hour_window,
    last_hour_window,
    minute_cells,
    window_utc,
)
from app.utils.timefmt import fmt_local, parse_utc

MAX_COLS = 40


def is_priority(chat: Chat) -> bool:
    name = (chat.display_name or "").casefold()
    return any(k in name for k in get_settings().priority_keys)


async def _columns(store: Store) -> list[Chat]:
    chats = [c for c in await store.list_chats(enabled_only=True)]
    # важные первыми, дальше по названию
    chats.sort(key=lambda c: (not is_priority(c), (c.display_name or "").casefold()))
    return chats


def _max_pause(times: list[datetime], start: datetime, end: datetime) -> int:
    pts = sorted(times)
    if not pts:
        return int((end - start).total_seconds() // 60)
    gaps = [(pts[0] - start)] + [b - a for a, b in zip(pts, pts[1:])] + [(end - pts[-1])]
    return int(max(gaps).total_seconds() // 60)


async def _expected_by_chat(store: Store) -> dict[int, float]:
    out: dict[int, float] = {}
    for cov in await build_coverage(store):
        out[cov.chat_pk] = expected_per_hour(len(cov.working), cov.interval)
    return out


async def _freshness(store: Store) -> str:
    last = await store.last_send_at()
    scans = await store.list_scans()
    if not scans:
        return f"{pe('warn')} Данных нет — нажмите «Собрать сейчас»."
    newest = max((parse_utc(s[2]) for s in scans if parse_utc(s[2])), default=None)
    return f"{pe('info')} Последний сбор: {fmt_local(newest)}; последняя отправка: {fmt_local(last)}"


async def window_view(
    store: Store,
    start_local: datetime,
    minutes: int,
    title: str,
) -> tuple[str, bytes | None]:
    tz = get_settings().tz
    chats = await _columns(store)
    accounts = {a.id: a for a in await store.list_accounts()}
    s_utc, e_utc = window_utc(start_local, minutes)
    events = await store.send_events_between(s_utc, e_utc)
    raw = minute_cells(events, start_local, minutes, tz)
    col_idx = {c.id: i for i, c in enumerate(chats[:MAX_COLS])}

    cells: dict[tuple[int, int], str] = {}
    for (row, chat_pk), acc_ids in raw.items():
        j = col_idx.get(chat_pk)
        if j is None:
            continue
        names = [accounts[a].label if a in accounts else f"#{a}" for a in acc_ids]
        cells[(row, j)] = names[0] if len(names) == 1 else f"{names[0]} +{len(names) - 1}"

    base = start_local.astimezone(tz).replace(second=0, microsecond=0)
    row_labels = [(base + timedelta(minutes=i)).strftime("%H:%M") for i in range(minutes)]
    end_local = base + timedelta(minutes=minutes)
    subtitle = (
        f"{base.strftime('%d.%m %H:%M')} – {end_local.strftime('%H:%M')} "
        f"({get_settings().timezone}) · отправок: {len(events)}"
    )
    # серым — минуты, по которым данных ещё нет (сбор раз в час / сбор был позже)
    from app.jobs import runtime

    scans = await store.list_scans()
    collected_until = max((parse_utc(sc[2]) for sc in scans if parse_utc(sc[2])), default=None)
    first_event = parse_utc(await store.first_send_at())
    grey: set[int] = set()
    for i in range(minutes):
        t = (base + timedelta(minutes=i)).astimezone(timezone.utc)
        if collected_until is None or t > collected_until:
            grey.add(i)
        elif first_event is not None and t < first_event:
            grey.add(i)
    collected_minutes = max(0, minutes - len(grey))
    collecting = runtime.is_running("facts", 0)
    footer = (
        "Закрашено — отправка подтверждена историей чата; пусто — не отправлено; "
        "серое — данные ещё не собраны"
        if grey
        else ""
    )
    if grey:
        subtitle += (
            f" · данные собраны до "
            f"{fmt_local(collected_until, '%H:%M') if collected_until else '—'} "
            f"({collected_minutes}/{minutes} мин)"
        )
    if collecting:
        subtitle += " · идёт сбор…"
    png = try_grid_png(
        title,
        subtitle,
        [c.display_name for c in chats[:MAX_COLS]],
        row_labels,
        cells,
        row_head="Время",
        footer=footer,
        grey_rows=grey,
    )

    per_chat: dict[int, list[datetime]] = {}
    for _a, chat_pk, sent_at in events:
        dt = parse_utc(sent_at)
        if dt:
            per_chat.setdefault(chat_pk, []).append(dt.astimezone(tz))
    expected = await _expected_by_chat(store)
    # Норму считаем только по уже собранным минутам — иначе «10 из 574» пугает впустую.
    factor = (collected_minutes / 60.0) if minutes else 0.0
    known_end = (
        collected_until.astimezone(tz)
        if collected_until is not None
        else base
    )
    known_end = min(known_end, end_local)
    if known_end < base:
        known_end = base

    lines = [
        f"{pe('chart')} <b>{escape(title)}</b>",
        f"{escape(subtitle)}",
    ]
    if collecting or (grey and collected_minutes < minutes):
        lines.append(
            f"{pe('warn')} Окно ещё не дочитано — серое ≠ «не отправили». "
            f"Норма ниже пересчитана на собранные {collected_minutes} мин."
        )
    exp_total = sum(expected.get(c.id, 0.0) for c in chats) * factor
    lines.append(
        f"Всего: <b>{len(events)}</b> в <b>{len(per_chat)}</b> из {len(chats)} чатов "
        f"(ожидалось ≈{exp_total:.0f}"
        + (f" за {collected_minutes} мин" if collected_minutes < minutes else "")
        + ")"
    )
    for chat in chats:
        if not is_priority(chat):
            continue
        times = per_chat.get(chat.id, [])
        exp = expected.get(chat.id, 0.0) * factor
        pause = _max_pause(times, base, known_end if known_end > base else end_local)
        lines.append(
            f"{pe('star')} <b>{escape(chat.display_name)}</b>: {len(times)} "
            f"(ожид. ≈{exp:.0f}) · макс. пауза {pause} мин"
        )
    # «Без отправок» только когда окно уже собрано — иначе ложная тревога.
    if collected_minutes >= minutes and not collecting:
        silent = [c.display_name for c in chats if c.id not in per_chat]
        if silent:
            names = ", ".join(escape(n) for n in silent[:6])
            more = f" +{len(silent) - 6}" if len(silent) > 6 else ""
            lines.append(f"{pe('block')} Без отправок: {names}{more}")
    if len(chats) > MAX_COLS:
        lines.append(f"<i>Показаны первые {MAX_COLS} чатов из {len(chats)}</i>")
    lines.append(await _freshness(store))
    return "\n".join(lines), png


async def last_hour_view(store: Store) -> tuple[str, bytes | None]:
    tz = get_settings().tz
    start = last_hour_window(datetime.now(timezone.utc), tz)
    return await window_view(store, start, 60, "Факт отправки: последний час")


async def hour_view(store: Store, days_ago: int, hour: int) -> tuple[str, bytes | None]:
    tz = get_settings().tz
    base = day_start(datetime.now(timezone.utc), tz, days_ago)
    start = hour_window(base, hour, tz)
    return await window_view(
        store, start, 60, f"Факт отправки: {start.strftime('%d.%m')} {hour:02d}:00"
    )


async def day_view(store: Store, days_ago: int) -> tuple[str, bytes | None, list[int]]:
    """Сутки: строки — часы, ячейка — число отправок. Возвращает и счётчик по часам."""
    tz = get_settings().tz
    chats = await _columns(store)
    base = day_start(datetime.now(timezone.utc), tz, days_ago)
    s_utc, e_utc = window_utc(base, 24 * 60)
    events = await store.send_events_between(s_utc, e_utc)
    counts = hour_counts(events, base, tz)
    col_idx = {c.id: i for i, c in enumerate(chats[:MAX_COLS])}
    cells: dict[tuple[int, int], str] = {}
    per_hour = [0] * 24
    for (hour, chat_pk), n in counts.items():
        per_hour[hour] += n
        j = col_idx.get(chat_pk)
        if j is not None:
            cells[(hour, j)] = str(n)
    title = f"Факт отправки за {base.strftime('%d.%m.%Y')}"
    png = try_grid_png(
        title,
        f"Число отправок по часам · всего {len(events)}",
        [c.display_name for c in chats[:MAX_COLS]],
        [f"{h:02d}:00" for h in range(24)],
        cells,
        row_head="Час",
        footer="Число — сколько сообщений аккаунты оставили в чате за этот час; пусто — 0",
    )
    per_chat: dict[int, int] = {}
    for (_h, chat_pk), n in counts.items():
        per_chat[chat_pk] = per_chat.get(chat_pk, 0) + n
    lines = [
        f"{pe('chart')} <b>{escape(title)}</b>",
        f"Всего отправок: <b>{len(events)}</b> · чатов с отправками: "
        f"<b>{len(per_chat)}</b> из {len(chats)}",
    ]
    for chat in chats:
        if is_priority(chat):
            lines.append(
                f"{pe('star')} <b>{escape(chat.display_name)}</b>: {per_chat.get(chat.id, 0)}"
            )
    quiet_hours = [f"{h:02d}" for h in range(24) if per_hour[h] == 0]
    if quiet_hours and days_ago > 0:
        lines.append(f"{pe('block')} Часы без отправок: {', '.join(quiet_hours)}")
    lines.append(await _freshness(store))
    return "\n".join(lines), png, per_hour


async def days_summary(store: Store, n: int = 7) -> list[tuple[int, str, int]]:
    """[(days_ago, 'дд.мм', всего отправок)]."""
    tz = get_settings().tz
    out: list[tuple[int, str, int]] = []
    now = datetime.now(timezone.utc)
    for d in range(n):
        base = day_start(now, tz, d)
        s_utc, e_utc = window_utc(base, 24 * 60)
        total = len(await store.send_events_between(s_utc, e_utc))
        out.append((d, base.strftime("%d.%m"), total))
    return out
