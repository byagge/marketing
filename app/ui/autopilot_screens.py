"""Экраны: чаты аккаунта (выключатели/тексты), автопилот, диагностика."""

from __future__ import annotations

from html import escape

from app.models import Account, AccountChatPref, AccountPerf, Chat, ChatBan, JoinState, SpamState
from app.ui.emoji import pe
from app.utils.send_policy import effective_interval_minutes

JOIN_STATUS_TEXT = {
    "member": "в чате",
    "pending": "ждёт вступления",
    "requested": "заявка отправлена",
    "manual": "нужно вручную",
    "abandoned": "автопилот сдался",
}


def pair_mark(pref: AccountChatPref | None, ban: ChatBan | None) -> str:
    if ban is not None and ban.active:
        return "🚫"
    if pref is not None and not pref.enabled:
        return "⛔"
    return "✅"


def pair_list_html(
    acc: Account,
    shown: int,
    total: int,
    query: str,
    off_n: int,
    text_n: int,
    ban_n: int,
) -> str:
    sender = "включён" if acc.sender_on else "ВЫКЛЮЧЕН"
    q = f"\n{pe('search')} Поиск: <code>{escape(query)}</code> — найдено {shown} из {total}" if query else ""
    return (
        f"{pe('users')} <b>Чаты аккаунта {escape(acc.label)}</b>\n"
        f"{pe('cube')} Sender аккаунта: <b>{sender}</b>\n"
        f"✅ шлёт · ⛔ выключено вручную ({off_n}) · 🚫 бан ({ban_n}) · ✎ свой текст ({text_n})"
        f"{q}\n\n"
        f"Выберите чат, чтобы включить/выключить отправку или задать свой текст."
    )


def pair_card_html(
    acc: Account,
    chat: Chat,
    pref: AccountChatPref,
    ban: ChatBan | None,
    join: JoinState | None,
) -> str:
    kind = "schedule" if chat.is_schedule else "sender"
    send = f"{pe('check')} включена" if pref.enabled else f"{pe('block')} <b>ВЫКЛЮЧЕНА</b>"
    lines = [
        f"{pe('user')} <b>{escape(acc.label)}</b> → {pe('mega')} <b>{escape(chat.display_name)}</b> ({kind})",
        f"Отправка: {send}",
    ]
    if kind == "sender" and not acc.sender_on:
        lines.append(f"{pe('warn')} Sender у аккаунта выключен целиком — этот чат не шлёт.")
    if ban is not None and ban.active:
        lines.append(
            f"{pe('warn')} <b>Бан:</b> {escape(ban.reason)} · {escape((ban.detected_at or '')[:16])} UTC"
            + (f"\n<code>{escape(ban.detail[:200])}</code>" if ban.detail else "")
        )
    if pref.has_text:
        prev = pref.text.strip().replace("\n", " ")
        prev = prev[:160] + ("…" if len(prev) > 160 else "")
        lines.append(f"{pe('bookmark')} Свой текст: <b>есть</b> ({len(pref.text)} симв., без фото)\n<i>{escape(prev)}</i>")
    else:
        lines.append(f"{pe('bookmark')} Свой текст: нет — берётся пост аккаунта")
    if join is not None:
        lines.append(
            f"{pe('users')} Вступление: {escape(JOIN_STATUS_TEXT.get(join.status, join.status))}"
            + (f" · <code>{escape(join.last_error[:120])}</code>" if join.last_error else "")
        )
    limit = int(chat.max_posts_per_account or 0)
    if limit:
        lines.append(
            f"{pe('clock')} Лимит чата: {limit} постов/аккаунт/сутки "
            f"(интервал не меньше {effective_interval_minutes(chat)} мин)"
        )
    return "\n".join(lines)


def autopilot_html(
    enabled: bool,
    last_at: str,
    last_summary: str,
    open_bans: int,
    running: bool,
    outreach_total: int = 0,
    outreach_limited: int = 0,
) -> str:
    state = f"{pe('check')} <b>включён</b>" if enabled else f"{pe('block')} <b>выключен</b>"
    run = f"\n{pe('robot')} Сейчас идёт проход…" if running else ""
    when = escape((last_at or "—")[:16].replace("T", " ")) + (" UTC" if last_at else "")
    summary = escape((last_summary or "").strip()[:1500]) or "—"
    return (
        f"{pe('robot')} <b>Автопилот</b>: {state}{run}\n\n"
        f"Сам вступает в чаты, где аккаунта нет, настраивает отправку и пишет вам про баны "
        f"и всё, где нужна ваша помощь.\n"
        f"{pe('warn')} Активных банов в базе: <b>{open_bans}</b>\n"
        + (
            f"🧲 Аутрич-аккаунтов: <b>{outreach_total}</b>, в спамблоке сейчас: "
            f"<b>{outreach_limited}</b> (в сводки не шумят)\n"
            if outreach_total
            else ""
        )
        +
        f"{pe('clock')} Последний проход: {when}\n\n"
        f"<b>Последняя сводка:</b>\n{summary}"
    )


def campaign_html(
    acc: Account,
    state: SpamState,
    load: int,
    schedule_n: int,
    dead_strikes: int = 3,
) -> str:
    """Строка «в кампании ли аккаунт»: sender / schedule, спамблок, dead."""
    sched = f"schedule: {schedule_n} чатов" if schedule_n else "schedule: нет настроенных чатов"
    strikes = f" · спамблоков было: {state.strikes}/{dead_strikes}" if state.strikes else ""
    head = f"{pe('mega')} <b>Кампания:</b> "
    if acc.is_outreach:
        block = ""
        if state.is_limited:
            since = escape((state.limited_since or "")[:16].replace("T", " "))
            block = f"; сейчас спамблок с {since} UTC — это нормально для аутрича"
        counted = f", спамблоков было: {state.strikes}" if state.strikes else ""
        return (
            f"{head}🧲 <b>АУТРИЧ</b> (мягкий режим) — sender не используется, только "
            f"{escape(sched)}{escape(block)}{escape(counted)}"
        )
    if acc.is_dead:
        return (
            f"{head}☠ <b>DEAD</b> — sender выключен навсегда, работает только "
            f"{escape(sched)}{escape(strikes)}"
        )
    if not acc.sender_on:
        return f"{head}⏸ <b>sender отключён вручную</b> — в кампании только {escape(sched)}"
    if state.is_limited:
        since = escape((state.limited_since or "")[:16].replace("T", " "))
        until = (
            f", Telegram снимет до {escape(state.limited_until[:16].replace('T', ' '))} UTC"
            if state.limited_until
            else ""
        )
        stop = "sender остановлен" if load <= 0 else f"sender {load}%"
        return (
            f"{head}🛡 <b>спамблок</b> с {since} UTC{until} — {stop}; "
            f"{escape(sched)} без изменений{escape(strikes)}"
        )
    if not acc.has_sender:
        return f"{head}ℹ sender не настроен — в кампании только {escape(sched)}"
    if load < 100:
        return (
            f"{head}🔄 <b>восстановление после спамблока</b> — sender {load}%, "
            f"{escape(sched)}{escape(strikes)}"
        )
    return f"{head}✅ <b>в кампании</b> — sender 100% + {escape(sched)}{escape(strikes)}"


def stoplist_html(keywords: tuple[str, ...], flagged: list[str]) -> str:
    words = ", ".join(f"<code>{escape(k)}</code>" for k in keywords) or "—"
    chats = "\n".join(f"· {escape(t)}" for t in flagged[:30]) or "—"
    more = f"\n· … ещё {len(flagged) - 30}" if len(flagged) > 30 else ""
    return (
        f"{pe('block')} <b>Стоп-лист: писать нельзя</b>\n\n"
        f"Чаты, в которые писать нельзя (за это бан), — например «Отзывы». "
        f"Туда не вступаем, не планируем и не шлём ни с одного аккаунта; "
        f"если sender где-то уже активен — выключаем.\n\n"
        f"{pe('search')} <b>Слова в названии:</b> {words}\n"
        f"{pe('pin')} <b>Сейчас под запретом (каталог):</b>\n{chats}{more}\n\n"
        f"Отдельный чат можно пометить на его карточке: «Писать нельзя»."
    )


def perf_line_html(perf: AccountPerf) -> str:
    """Строка карточки аккаунта: сколько людей пишут и сколько раз ответил клоакинг."""
    if not perf.measured:
        return ""
    cloak = "—" if perf.cloak_n < 0 else str(perf.cloak_n)
    flag = " · 🎨 <b>для переоформления</b>" if perf.flagged else ""
    return (
        f"\n{pe('chart')} Клиенты за {perf.window_days} дн.: написали <b>{perf.wrote_n}</b> "
        f"(первыми {perf.new_n}), клоакинг сработал <b>{escape(cloak)}</b>{flag}"
    )
