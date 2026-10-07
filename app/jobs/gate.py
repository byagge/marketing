"""Обход «ворот подписки»: аккаунт сам подписывается на каналы, которые требует бот чата."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import Account, Chat
from app.store import Store
from app.tg.gate import merge_require_channels, resolve_gate, scan_gate
from app.tg.join_flows import subscribe_required
from app.tg.resolve import lookup_entity

# Сколько раз подряд подписываемся ради одного чата, прежде чем сказать «не помогает».
MAX_GATE_RESOLVES = 3


@dataclass
class GateEvent:
    chat: str
    kind: str  # resolved | unresolved
    detail: str


def _hours_since(ts: str, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 3600


async def gate_step(
    store: Store,
    client,
    acc: Account,
    chats: list[Chat],
    *,
    budget: int = 6,
    recheck_hours: float = 12.0,
    now: datetime | None = None,
    pause: float = 0.5,
) -> list[GateEvent]:
    """
    Для чатов, где аккаунт состоит: найти сообщение-ворота и подписаться.

    На каждый проход — не больше `budget` чатов; чат перепроверяется раз в `recheck_hours`.
    Найденные каналы дописываются в «обязательные» чата — остальные аккаунты
    подписываются на них при первой проверке.
    """
    moment = now or datetime.now(timezone.utc)
    states = await store.join_states_for_account(acc.id)

    def due(chat: Chat) -> bool:
        st = states.get(chat.id)
        if st is None or not st.gate_at:
            return True
        hours = _hours_since(st.gate_at, moment)
        return hours is None or hours >= recheck_hours

    todo = sorted(
        (c for c in chats if due(c)),
        key=lambda c: (states[c.id].gate_at if c.id in states else ""),
    )[: max(0, budget)]
    if not todo:
        return []

    me = await client.get_me()
    events: list[GateEvent] = []
    for i, chat in enumerate(todo):
        if i:
            await asyncio.sleep(pause)
        st = states.get(chat.id)
        first_time = st is None or not st.gate_at
        title = chat.display_name
        try:
            entity = await lookup_entity(client, chat)
            if entity is None:
                await store.mark_gate(acc.id, chat.id, note="чат не найден")
                continue
            if first_time and chat.require_channels.strip():
                await subscribe_required(client, chat.require_channels)
            info = await scan_gate(client, entity, me, chat.username)
            if info is None:
                await store.mark_gate(acc.id, chat.id, note="ворот нет")
                continue
            if st is not None and info.message_id == st.gate_msg_id:
                await store.mark_gate(acc.id, chat.id, msg_id=info.message_id, note=st.gate_note)
                continue
            if st is not None and st.gate_count >= MAX_GATE_RESOLVES:
                if not st.gate_note.startswith("unresolved"):
                    events.append(
                        GateEvent(
                            title,
                            "unresolved",
                            f"бот снова требует подписку после {st.gate_count} попыток: "
                            f"{', '.join(info.links)[:150]}",
                        )
                    )
                await store.mark_gate(
                    acc.id, chat.id, msg_id=info.message_id, note="unresolved: ворота повторяются"
                )
                continue
            result = await resolve_gate(client, entity, info)
            merged = merge_require_channels(chat.require_channels, info.links)
            if merged != chat.require_channels:
                await store.update_chat(chat.id, require_channels=merged)
            note = ", ".join(result.notes)[:200]
            await store.mark_gate(
                acc.id, chat.id, msg_id=info.message_id, note=note, resolved=True
            )
            if result.subscribed:
                extra = f", нажал «{result.confirmed}»" if result.confirmed else ""
                events.append(GateEvent(title, "resolved", f"{note}{extra}"))
            else:
                events.append(GateEvent(title, "unresolved", f"подписаться не вышло: {note}"))
        except Exception as e:  # один чат не должен ломать обход остальных
            await store.mark_gate(acc.id, chat.id, note=f"ошибка: {type(e).__name__}")
    return events
