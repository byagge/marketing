"""Экраны «Муты» и «Баны»: что сейчас активно, где, когда и почему."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from app.jobs.restr_events import KIND_ICON, SOURCE_TEXT
from app.models import RestrEvent
from app.store import Store
from app.utils.timefmt import fmt_local, fmt_until

REASON_LIMIT = 320


@dataclass
class Entry:
    kind: str  # mute | nowrite | spamblock | ban
    account_id: int
    chat_pk: int
    account_label: str
    chat_title: str
    detected_at: str = ""
    until_at: str = ""
    source: str = ""
    reason: str = ""  # что записано на момент обнаружения (отметка бота и т.п.)
    link: str = ""
    event: RestrEvent | None = None

    @property
    def why(self) -> str:
        ev = self.event
        if ev is not None and ev.analyzed and ev.summary:
            return ev.summary
        return self.reason

    @property
    def link_url(self) -> str:
        if self.event is not None and self.event.link:
            return self.event.link
        return self.link


async def mute_entries(store: Store) -> list[Entry]:
    events = await store.latest_restr_events()
    out: list[Entry] = []
    for r in await store.list_restrictions(kinds=("mute", "nowrite", "spamblock")):
        cls = "spamblock" if r.kind == "spamblock" else "mute"
        out.append(
            Entry(
                kind=r.kind,
                account_id=r.account_id,
                chat_pk=r.chat_pk,
                account_label=r.account_label,
                chat_title=r.chat_title,
                detected_at=r.detected_at,
                until_at=r.until_at,
                reason=r.reason,
                link=r.reason_link,
                event=events.get((r.account_id, r.chat_pk, cls)),
            )
        )
    out.sort(key=lambda e: e.detected_at, reverse=True)  # новые сверху
    return out


async def ban_entries(store: Store) -> list[Entry]:
    """Баны из статуса участника и из базы банов (вылет / бан на вступлении / на отправке)."""
    events = await store.latest_restr_events()
    out: dict[tuple[int, int], Entry] = {}
    for r in await store.list_restrictions(kinds=("ban",)):
        out[(r.account_id, r.chat_pk)] = Entry(
            kind="ban",
            account_id=r.account_id,
            chat_pk=r.chat_pk,
            account_label=r.account_label,
            chat_title=r.chat_title,
            detected_at=r.detected_at,
            source="restriction",
            reason=r.reason,
            link=r.reason_link,
            event=events.get((r.account_id, r.chat_pk, "ban")),
        )
    for b in await store.list_bans(active_only=True):
        key = (b.account_id, b.chat_pk)
        if key in out:
            if out[key].source == "restriction" and b.reason:
                out[key].source = b.reason
            continue
        out[key] = Entry(
            kind="ban",
            account_id=b.account_id,
            chat_pk=b.chat_pk,
            account_label=b.account_label,
            chat_title=b.chat_title,
            detected_at=b.detected_at,
            source=b.reason,
            reason=b.detail,
            event=events.get((b.account_id, b.chat_pk, "ban")),
        )
    items = list(out.values())
    items.sort(key=lambda e: e.detected_at, reverse=True)
    return items


def entry_html(num: int, e: Entry) -> str:
    icon = KIND_ICON.get(e.kind, "⚠")
    lines = [
        f"<b>{num}.</b> {icon} <b>{escape(e.account_label or str(e.account_id))}</b> → "
        f"«{escape(e.chat_title or str(e.chat_pk))}»"
    ]
    if e.kind == "ban":
        when = f"   с {fmt_local(e.detected_at)}"
        if e.source:
            when += f" · {escape(SOURCE_TEXT.get(e.source, e.source))}"
    else:
        word = {"spamblock": "SpamBlock", "nowrite": "чат закрыт"}.get(e.kind, "мут")
        when = f"   {word}, с {fmt_local(e.detected_at)} до {escape(fmt_until(e.until_at))}"
    lines.append(when)
    why = e.why
    if why:
        text = why if len(why) <= REASON_LIMIT else why[: REASON_LIMIT - 1] + "…"
        lines.append(f"   <i>Почему:</i> {escape(text)}")
    elif e.event is not None and not e.event.analyzed:
        lines.append("   <i>Почему: разбираю…</i>")
    else:
        lines.append("   <i>Почему: не разобрано — кнопка «Разобрать причины».</i>")
    if e.link_url:
        lines.append(f'   <a href="{escape(e.link_url)}">сообщение в чате</a>')
    return "\n".join(lines)
