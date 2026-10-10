"""Журнал мутов и банов: разбор причины и уведомление админу.

Запись в журнал создаётся в Store при каждом НОВОМ муте/бане (upsert_restriction / record_ban).
Здесь — вторая половина: найти причину в Telegram (чат, отметки аккаунта, боты-гаранты,
правила чата, наша отправка перед мутом) и один раз написать админу.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from app.models import Account, RestrEvent
from app.notify import safe_send
from app.store import Store
from app.tg.client import telethon_client
from app.tg.mutewhy import CAUSE_LABEL, Gathered, Ident, Why, build_why, gather
from app.tg.resolve import lookup_entity
from app.utils.errfmt import short_error
from app.utils.timefmt import fmt_local, fmt_until, parse_utc

log = logging.getLogger("marketing.restr_events")

MAX_ATTEMPTS = 3
PARALLEL_ACCOUNTS = 3
DETAILED_NOTICES = 6  # больше — присылаем сводкой
HELPERS_PER_BAN = 2

KIND_ICON = {"mute": "🔇", "nowrite": "🔒", "ban": "🚫", "spamblock": "⛔"}
KIND_WORD = {
    "mute": "Мут",
    "nowrite": "Чат закрыт для записи",
    "ban": "Бан",
    "spamblock": "SpamBlock аккаунта",
}
SOURCE_TEXT = {
    "join_ban": "не пускает (бан на вступлении)",
    "removed": "вылетел / исключён из чата",
    "send_ban": "бан на отправку сообщений",
    "restriction": "исключён из чата (статус участника)",
}

# что делать — только законные варианты, без попыток обойти модерацию чата
ADVICE = {
    "antispam": (
        "чат считает сообщения авторассылкой. Не обходите это — лучше выключить чат "
        "для рассылки (кнопка ниже), взять у чата официальный автопостинг или публиковать вручную."
    ),
    "duplicate": "в чате не любят повторы: смените текст или выключите чат для рассылки.",
    "frequency": "в чате ограничена частота: реже писать или выключить чат для рассылки.",
    "links": "в чате нельзя ссылки/упоминания: проверьте текст поста под правила чата.",
    "ads": "реклама в чате ограничена: проверьте, в каком разделе/теме разрешено, или исключите чат.",
    "verify": "аккаунт не прошёл проверку: зайдите в чат вручную, пройдите капчу/подписку.",
    "rules": "сверьте пост с правилами чата или исключите чат.",
    "admin": "это решение админов — договаривайтесь с ними или исключите чат.",
    "notice": "прочитайте сообщение выше — там сказано, что требует чат.",
    "pattern": "если мут повторяется после каждой отправки — выключите чат для рассылки.",
    "not_found": "если мут будет повторяться в этом чате, лучше спросить админов или выключить чат.",
}
CAN_DISABLE_CHAT = {
    "antispam", "duplicate", "frequency", "links", "ads", "rules", "admin",
    "notice", "pattern", "not_found", "unreadable",
}


def _dur(ev: RestrEvent) -> str:
    det, until = parse_utc(ev.detected_at), parse_utc(ev.until_at)
    if not det or not until or until <= det:
        return ""
    hours = (until - det).total_seconds() / 3600
    if hours < 48:
        return f" (на {hours:.0f} ч)"
    return f" (на {hours / 24:.0f} дн)"


def event_html(ev: RestrEvent, *, full: bool = True) -> str:
    """Карточка записи журнала для бота."""
    icon = KIND_ICON.get(ev.kind, "⚠")
    word = KIND_WORD.get(ev.kind, ev.kind)
    head = (
        f"{icon} <b>{word}</b> · <b>{escape(ev.account_label or str(ev.account_id))}</b> → "
        f"«{escape(ev.chat_title or str(ev.chat_pk))}»"
    )
    lines = [head]
    when = f"Когда: {fmt_local(ev.detected_at)}"
    if ev.kind != "ban":
        when += f" · до: {escape(fmt_until(ev.until_at))}{_dur(ev)}"
    lines.append(when)
    if ev.kind == "ban" and ev.source:
        lines.append(f"Как обнаружен: {escape(SOURCE_TEXT.get(ev.source, ev.source))}")
    if ev.analyzed and ev.summary:
        lines.append(f"<b>Почему:</b> {escape(ev.summary)}")
    else:
        lines.append("<b>Почему:</b> <i>ещё разбираю…</i>")
    if full and ev.evidence:
        lines.append("<b>Что нашёл:</b>")
        lines.append(escape(ev.evidence))
    if ev.link:
        lines.append(f'<a href="{escape(ev.link)}">Сообщение в чате</a>')
    if full:
        advice = ADVICE.get(ev.cause)
        if advice and ev.kind != "ban":
            lines.append(f"<b>Что делать:</b> {escape(advice)}")
        elif ev.kind == "ban":
            lines.append(
                "<b>Что делать:</b> автопилот эту пару больше не трогает; вернуться можно "
                "только договорившись с админом чата."
            )
    return "\n".join(lines)


def event_kb(ev: RestrEvent) -> InlineKeyboardMarkup:
    from app.bot.keyboards import ib

    rows = []
    if ev.kind != "ban" and ev.cause in CAN_DISABLE_CHAT:
        rows.append([ib("Не писать в этот чат", "ev_nopost", ev.id, icon="block")])
    rows.append(
        [
            ib("Все муты", "restr_mutes", icon="clock"),
            ib("Все баны", "restr_bans", icon="block"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def digest_html(events: list[RestrEvent]) -> str:
    lines = [f"⚠ <b>Новые муты / баны: {len(events)}</b>", ""]
    for ev in events[:25]:
        icon = KIND_ICON.get(ev.kind, "⚠")
        why = CAUSE_LABEL.get(ev.cause, ev.cause or "причина не определена")
        lines.append(
            f"{icon} {escape(ev.account_label or str(ev.account_id))} → "
            f"«{escape(ev.chat_title or str(ev.chat_pk))}»: {escape(why)}"
        )
    if len(events) > 25:
        lines.append(f"… ещё {len(events) - 25}")
    lines.append("\nПодробности: «Баны / муты» → Муты / Баны.")
    return "\n".join(lines)


# ---- разбор ------------------------------------------------------------------------------


async def _ident(client: Any, acc: Account) -> Ident:
    try:
        me = await client.get_me()
    except Exception:  # noqa: BLE001
        return Ident(username=(acc.username or "").lstrip("@"))
    first = (getattr(me, "first_name", "") or "").strip()
    last = (getattr(me, "last_name", "") or "").strip()
    names = (f"{first} {last}".strip(),) if first and last else ((first,) if first else ())
    return Ident(
        user_id=int(getattr(me, "id", 0) or 0),
        username=(getattr(me, "username", "") or acc.username or "").lstrip("@"),
        names=names,
    )


def _extra_bots(chat: Any) -> set[str]:
    return {
        (getattr(chat, name, "") or "").lstrip("@").casefold()
        for name in ("garant_bot", "after_join_bot")
        if getattr(chat, name, "")
    }


async def _read_via_helpers(
    store: Store, acc: Account, chat: Any, ident: Ident, bots: set[str]
) -> Gathered | None:
    """Бан: сам аккаунт чат уже не читает — смотрим глазами другого нашего аккаунта."""
    blocked = {r.account_id for r in await store.list_restrictions(chat_pk=chat.id)}
    blocked |= {b.account_id for b in await store.list_bans(active_only=True) if b.chat_pk == chat.id}
    tried = 0
    for helper in await store.list_accounts():
        if helper.id == acc.id or helper.id in blocked or not helper.telethon_session:
            continue
        if tried >= HELPERS_PER_BAN:
            break
        tried += 1
        try:
            async with telethon_client(helper.telethon_session) as client:
                entity = await lookup_entity(client, chat)
                if entity is None:
                    continue
                got = await gather(client, entity, ident, self_reader=False, extra_bots=bots)
                if got.readable:
                    return got
        except Exception as e:  # noqa: BLE001
            log.info("helper read failed (%s): %s", helper.label, e)
    return None


async def _analyze_one(
    store: Store, client: Any, acc: Account, ident: Ident, ev: RestrEvent
) -> Why:
    chat = await store.get_chat(ev.chat_pk)
    if chat is None:
        return Why("not_found", "Чат уже удалён из каталога.")
    bots = _extra_bots(chat)
    entity = await lookup_entity(client, chat)
    if entity is None:
        got = Gathered(readable=False, read_error="аккаунт не видит чат")
    else:
        got = await gather(client, entity, ident, self_reader=True, extra_bots=bots)
    if not got.readable and ev.kind == "ban":
        via = await _read_via_helpers(store, acc, chat, ident, bots)
        if via is not None:
            got = via
            got.read_error = (
                got.read_error or "прочитано через другой наш аккаунт (этот уже не видит чат)"
            )
    detected = parse_utc(ev.detected_at)
    sends: list = []
    if detected is not None:
        start = (detected - timedelta(hours=24)).isoformat(timespec="seconds")
        parsed = [parse_utc(s) for s in await store.send_times(acc.id, chat.id, start)]
        sends = [s for s in parsed if s is not None]
    return build_why(
        ev.kind,
        gathered=got,
        sends=sends,
        detected=detected,
        until=parse_utc(ev.until_at),
    )


async def _analyze_account(store: Store, acc: Account, events: list[RestrEvent]) -> int:
    done = 0
    pending = list(events)
    err = ""
    if acc.telethon_session:
        try:
            async with telethon_client(acc.telethon_session) as client:
                ident = await _ident(client, acc)
                while pending:
                    ev = pending[0]
                    try:
                        why = await _analyze_one(store, client, acc, ident, ev)
                    except Exception as e:  # noqa: BLE001
                        log.warning("restr analyze failed %s/%s: %s", acc.label, ev.chat_pk, e)
                        err = short_error(e)
                        await _fail(store, ev, err)
                        pending.pop(0)
                        continue
                    await store.save_restr_analysis(
                        ev.id,
                        cause=why.cause,
                        summary=why.summary,
                        evidence=why.evidence_text,
                        link=why.link,
                    )
                    pending.pop(0)
                    done += 1
        except Exception as e:  # noqa: BLE001
            err = short_error(e)
            log.warning("restr analyze: клиент %s не открылся: %s", acc.label, err)
    else:
        err = "у аккаунта нет Telethon-сессии"
    for ev in pending:
        await _fail(store, ev, err or "не удалось открыть аккаунт")
    return done


async def _fail(store: Store, ev: RestrEvent, err: str) -> None:
    attempts = await store.bump_restr_attempt(ev.id)
    if attempts >= MAX_ATTEMPTS:
        await store.save_restr_analysis(
            ev.id,
            cause="unreadable",
            summary=f"Причину определить не получилось: {err}",
            evidence="",
        )


async def analyze_pending(store: Store, *, limit: int = 30) -> int:
    """Разобрать причины у записей журнала, где их ещё нет. Возвращает, сколько разобрано."""
    events = await store.pending_restr_events(limit=limit, max_attempts=MAX_ATTEMPTS)
    by_acc: dict[int, list[RestrEvent]] = {}
    for ev in events:
        by_acc.setdefault(ev.account_id, []).append(ev)
    sem = asyncio.Semaphore(PARALLEL_ACCOUNTS)

    async def _run(acc_id: int, items: list[RestrEvent]) -> int:
        acc = await store.get_account(acc_id)
        if acc is None:
            for ev in items:
                await store.save_restr_analysis(
                    ev.id, cause="not_found", summary="Аккаунт удалён."
                )
            return 0
        async with sem:
            return await _analyze_account(store, acc, items)

    results = await asyncio.gather(
        *(_run(a, items) for a, items in by_acc.items()), return_exceptions=True
    )
    done = 0
    for r in results:
        if isinstance(r, int):
            done += r
        else:
            log.warning("restr analyze batch failed: %s", r)
    return done


async def notify_new(store: Store, bot: Bot | None, admin_chat_id: int | None) -> int:
    """Написать админу про новые разобранные муты/баны (один раз по каждому)."""
    events = await store.unnotified_restr_events()
    if not events:
        return 0
    if bot is None or not admin_chat_id:
        return 0
    sent_ids: list[int] = []
    if len(events) <= DETAILED_NOTICES:
        for ev in events:
            ok = await safe_send(
                bot,
                admin_chat_id,
                event_html(ev),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=event_kb(ev),
            )
            if ok:
                sent_ids.append(ev.id)
    else:
        ok = await safe_send(
            bot,
            admin_chat_id,
            digest_html(events),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if ok:
            sent_ids = [e.id for e in events]
    await store.mark_restr_notified(sent_ids)
    return len(sent_ids)


async def run_restr_events(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    adopt: bool = False,
    notify: bool = True,
    limit: int = 30,
) -> str:
    """adopt=True — завести журнал и для уже существующих ограничений (тихо)."""
    adopted = await store.adopt_restr_events() if adopt else 0
    analyzed = await analyze_pending(store, limit=limit if not adopt else max(limit, 60))
    notified = await notify_new(store, bot, admin_chat_id) if notify else 0
    return (
        f"Муты/баны: заведено в журнал {adopted}, причин разобрано {analyzed}, "
        f"уведомлений {notified}"
    )
