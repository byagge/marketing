"""Отчёт по частоте: где сколько аккаунтов реально работает и сколько не хватает."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.store import Store
from app.utils.chat_ids import canon_chat_id
from app.utils.coverage import (
    TARGET_GAP_MIN,
    ChatCoverage,
    accounts_needed,
    max_gap,
    render_line,
)
from app.utils.timefmt import to_iso


async def build_coverage(store: Store, target_gap: int = TARGET_GAP_MIN) -> list[ChatCoverage]:
    chats = [c for c in await store.list_chats(kind="schedule") if c.enabled]
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    slots = await store.all_slots()
    states = {(s.account_id, s.chat_pk): s for s in await store.list_setup_states()}
    restr = {(r.account_id, r.chat_pk): r for r in await store.list_restrictions()}
    disabled = await store.disabled_pairs()
    scans = {(a, c): status for a, c, _t, status, _d in await store.list_scans()}
    now = datetime.now(timezone.utc)
    events = await store.send_events_between(
        to_iso(now - timedelta(hours=24)), to_iso(now + timedelta(minutes=1))
    )
    sent_by_chat: dict[int, int] = {}
    for _a, chat_pk, _t in events:
        sent_by_chat[chat_pk] = sent_by_chat.get(chat_pk, 0) + 1

    slot_map = {(s.account_id, s.chat_pk): s for s in slots}
    out: list[ChatCoverage] = []
    for chat in chats:
        cov = ChatCoverage(
            chat_pk=chat.id,
            title=chat.display_name,
            interval=chat.interval_minutes,
            needed=accounts_needed(chat.interval_minutes, target_gap),
            sent_24h=sent_by_chat.get(chat.id, 0),
        )
        work_minutes: list[int] = []
        for acc in accounts:
            key = (acc.id, chat.id)
            if (acc.id, canon_chat_id(chat.chat_id)) in disabled:
                cov.disabled.append(acc.label)
                continue
            r = restr.get(key)
            if r is not None:
                (cov.banned if r.is_ban else cov.muted).append(acc.label)
                continue
            slot = slot_map.get(key)
            state = states.get(key)
            if slot and (state is None or state.is_ok):
                cov.working.append(acc.label)
                work_minutes.append(slot.start_minute)
            elif slot or (state is not None and not state.is_ok):
                cov.broken.append(acc.label)
            else:
                if acc.is_spam_limited:
                    continue
                if scans.get(key) == "not_member":
                    cov.candidates_join.append(acc.label)
                else:
                    cov.candidates.append(acc.label)
        cov.gap = max_gap(work_minutes, 60)
        out.append(cov)
    out.sort(key=lambda c: (-c.deficit, c.title.casefold()))
    return out


async def coverage_report(store: Store, only: set[str] | None = None) -> str:
    items = await build_coverage(store)
    if only:
        items = [c for c in items if any(k in c.title.casefold() for k in only)]
    if not items:
        return "Нет schedule-чатов"
    lines = [f"Частота отправки (цель: сообщение в чат не реже раза в {TARGET_GAP_MIN} мин)", ""]
    bad = [c for c in items if c.deficit]
    for c in items:
        lines.append(("⚠ " if c.deficit else "✓ ") + render_line(c))
    lines.append("")
    lines.append(
        f"Не хватает аккаунтов в {len(bad)} из {len(items)} чатов; "
        f"всего нужно добавить ≈{sum(c.deficit for c in bad)}."
    )
    return "\n".join(lines)
