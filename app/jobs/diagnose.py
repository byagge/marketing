"""Диагностика чата: почему аккаунты в нём не отправляют."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from html import escape

from app.models import Account, Chat
from app.jobs.spam import account_load
from app.store import Store
from app.utils.send_policy import REASON_LABEL, is_no_post, is_send_allowed, pick_text_override
from app.utils.templates import pick_post_for_chat


@dataclass
class ChatDiagnosis:
    chat: Chat
    accounts_total: int = 0
    ok: list[str] = field(default_factory=list)
    reasons: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> int:
        return sum(len(v) for v in self.reasons.values())


async def diagnose_chat(
    store: Store,
    chat_pk: int,
    *,
    live_members: dict[int, bool | None] | None = None,
) -> ChatDiagnosis | None:
    """
    Разбор по данным системы: для каждого аккаунта — что мешает отправке в этот чат.

    live_members — результат живой проверки членства (account_id → True/False/None),
    если нужно учесть её; без неё членство берётся из состояния автовступления.
    """
    chat = await store.get_chat(chat_pk)
    if chat is None:
        return None
    accounts = await store.list_accounts()
    prefs = await store.prefs_for_chat(chat.id)
    bans = {b.account_id: b for b in await store.list_bans() if b.chat_pk == chat.id}
    slots = {s.account_id: s for s in await store.slots_for_chat(chat.id)}
    setup_states = {
        s.account_id: s for s in await store.list_setup_states() if s.chat_pk == chat.id
    }
    join_states = {
        s.account_id: s for s in await store.list_join_states() if s.chat_pk == chat.id
    }

    diag = ChatDiagnosis(chat=chat, accounts_total=len(accounts))
    keywords = await store.get_stoplist()
    if is_no_post(chat, keywords):
        diag.notes.append("В чат писать нельзя (флаг или стоп-лист) — отправка везде выключена намеренно.")

    if not chat.enabled:
        diag.notes.append("Чат выключен в каталоге — отправки нет ни у кого.")
    if chat.is_schedule and not slots:
        diag.notes.append(
            "В таблице минут у этого schedule-чата нет ни одного аккаунта («в плане 0») — "
            "отправлять некому. Добавьте аккаунты в таблицу или «Перенастроить все»."
        )
    if chat.kind == "sender":
        diag.notes.append(
            "Sender-чат: рассылка идёт из Autoposter и только у аккаунтов, которые "
            "сидят в чате и у которых чат активен. Таблица минут не используется."
        )
    if int(chat.max_posts_per_account or 0):
        diag.notes.append(
            f"Лимит чата: не более {chat.max_posts_per_account} постов на аккаунт в сутки."
        )

    for acc in accounts:
        why = _account_reason(
            acc,
            chat,
            prefs.get(acc.id),
            bans.get(acc.id),
            slots.get(acc.id),
            setup_states.get(acc.id),
            join_states.get(acc.id),
            live_members.get(acc.id) if live_members is not None else None,
            live_known=live_members is not None and acc.id in live_members,
            has_text=await _has_text(store, acc, chat, prefs.get(acc.id)),
            spam_load=await account_load(store, acc),
            stoplist=keywords,
        )
        if why:
            diag.reasons[why].append(acc.label)
        else:
            diag.ok.append(acc.label)
    return diag


async def _has_text(store: Store, acc: Account, chat: Chat, pref) -> bool:
    if pick_text_override(pref) is not None:
        return True
    posts = {
        lang: await store.get_post(acc.id, lang)
        for lang in ("ru", "en", "ru_short", "en_short")
    }
    post = pick_post_for_chat(posts, chat)
    return bool((post.text or "").strip() or post.has_text_link or post.has_photo_link)


def _account_reason(
    acc: Account,
    chat: Chat,
    pref,
    ban,
    slot,
    setup_state,
    join_state,
    live_member: bool | None,
    *,
    live_known: bool,
    has_text: bool,
    spam_load: int = 100,
    stoplist: tuple[str, ...] = (),
) -> str:
    allowed, why = is_send_allowed(
        acc, chat, pref, bool(ban and ban.active), spam_load=spam_load, stoplist=stoplist
    )
    if not allowed:
        label = REASON_LABEL.get(why, why)
        if why == "banned" and ban is not None:
            label += f" ({ban.reason})"
        return label
    if chat.is_schedule and not acc.telethon_session:
        return "нет Telethon session (schedule не планируется)"
    if chat.kind == "sender" and not acc.has_sender:
        return "нет sender (Sender ID / Pyrogram + token)"
    if not has_text:
        return "нет текста для этого чата (ни поста аккаунта, ни своего)"
    if live_known and live_member is False:
        return "не состоит в чате (проверка вживую)"
    if live_known and live_member is None:
        return "не удалось проверить членство (ошибка Telethon)"
    if join_state is not None and join_state.status in {"manual", "abandoned"}:
        return f"не вступил: {join_state.last_error or join_state.status}"
    if join_state is not None and join_state.gate_note.startswith("unresolved"):
        return "бот чата требует подписку, а подписка не помогает (нужна помощь)"
    if join_state is not None and join_state.status == "requested":
        return "заявка на вступление ждёт одобрения"
    if join_state is not None and join_state.status == "pending" and join_state.miss_count:
        return "не состоит в чате (автопилот вступает)"
    if chat.is_schedule:
        if slot is None:
            return "нет минуты в таблице (не в плане)"
        if setup_state is None:
            return "schedule ещё не настраивался"
        if setup_state.status != "ok":
            return f"schedule не настроен: {setup_state.last_error or setup_state.status}"
    return ""


def diagnosis_html(diag: ChatDiagnosis) -> str:
    chat = diag.chat
    kind = "schedule" if chat.is_schedule else "sender"
    lines = [
        f"🔎 <b>Диагностика: {escape(chat.display_name)}</b> ({kind})",
        f"Аккаунтов в системе: <b>{diag.accounts_total}</b> · "
        f"может слать: <b>{len(diag.ok)}</b> · не шлют: <b>{diag.blocked}</b>",
        "",
    ]
    for note in diag.notes:
        lines.append(f"ℹ {escape(note)}")
    if diag.notes:
        lines.append("")
    for reason, names in sorted(diag.reasons.items(), key=lambda kv: -len(kv[1])):
        shown = ", ".join(escape(n) for n in names[:14])
        extra = f" … +{len(names) - 14}" if len(names) > 14 else ""
        lines.append(f"✗ <b>{escape(reason)}</b> — {len(names)}: {shown}{extra}")
    if diag.ok:
        shown = ", ".join(escape(n) for n in diag.ok[:20])
        extra = f" … +{len(diag.ok) - 20}" if len(diag.ok) > 20 else ""
        lines.append(f"\n✓ Нет препятствий в системе ({len(diag.ok)}): {shown}{extra}")
        lines.append(
            "<i>Если они всё равно молчат — нажмите «Проверить вживую» "
            "(членство в чате по Telethon).</i>"
        )
    return "\n".join(lines)


async def live_membership(store: Store, chat_pk: int) -> dict[int, bool | None]:
    """Живая проверка: состоит ли каждый Telethon-аккаунт в чате (True/False/None=ошибка)."""
    from app.jobs.parallel import map_batches, setup_parallel_defaults
    from app.tg.client import telethon_client
    from app.tg.join import is_member

    chat = await store.get_chat(chat_pk)
    if chat is None:
        return {}
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    size, pause = setup_parallel_defaults()

    async def _one(acc: Account) -> tuple[int, bool | None]:
        try:
            async with telethon_client(acc.telethon_session) as client:
                return acc.id, await is_member(client, chat)
        except Exception:
            return acc.id, None

    out: dict[int, bool | None] = {}
    for item in await map_batches(accounts, _one, batch_size=size, batch_pause=pause):
        if isinstance(item, BaseException):
            continue
        out[item[0]] = item[1]
    return out
