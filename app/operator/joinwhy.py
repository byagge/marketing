"""Почему аккаунт не в чате — одной понятной фразой и с кодом причины.

Чистая логика (без Telegram и БД): на вход состояние вступления и факты, на выход —
причина, нужен ли человек и временная ли она. Нужна, чтобы в отчётах не было общего
«не вступлен/недоступен», а было: бан / заявка ждёт / спамблок / нет ссылки / сдался.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import Chat, JoinState
from app.tg.join import chat_join_target, is_closed_chat

BAN = "ban"  # бан в чате (вступлении / вылет / отправке)
REQUEST = "request"  # заявка отправлена, ждём одобрения админа
NO_LINK = "no_link"  # вступить нечем: ни invite, ни @username, ни гаранта
MANUAL = "manual"  # автоматом не выходит (бот / капча / особая ссылка)
SPAMBLOCK = "spamblock"  # аккаунт в спамблоке, а чат публичный
GAVE_UP = "gave_up"  # серия неудач, повтор по расписанию
WAIT = "wait"  # была неудача, ждём паузу между попытками
READY = "ready"  # ещё не пробовали / пора — вступит на ближайшем проходе

# причины, при которых вступление в обозримом будущем не ожидается без вмешательства
STUCK = {BAN, NO_LINK, MANUAL, SPAMBLOCK, GAVE_UP}


@dataclass
class JoinWhy:
    code: str
    text: str
    human: bool = False  # без человека не решается
    transient: bool = True  # пройдёт само (спамблок снимется, админ одобрит, пауза)


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%d.%m %H:%M UTC") if dt else "?"


def explain_not_member(
    chat: Chat,
    state: JoinState | None,
    *,
    banned: bool = False,
    ban_detail: str = "",
    spam_limited: bool = False,
    spam_until: str = "",
    retry_hours: float = 6.0,
    max_attempts: int = 5,
    abandoned_retry_hours: float = 72.0,
    now: datetime | None = None,
) -> JoinWhy:
    moment = now or datetime.now(timezone.utc)
    if banned:
        extra = f": {ban_detail[:80]}" if ban_detail else ""
        return JoinWhy(BAN, f"бан в чате{extra}", human=False, transient=False)

    method = chat_join_target(chat)["method"]
    if method in {"manual", "chat_id"} and not (chat.invite_link or chat.username):
        return JoinWhy(
            NO_LINK,
            "нет ссылки вступления и @username (по числовому id вступить нельзя)",
            human=True,
            transient=False,
        )

    last = _parse(state.last_attempt_at) if state else None
    err = ((state.last_error if state else "") or "").strip()

    if state is not None and state.status == "requested":
        return JoinWhy(
            REQUEST,
            f"заявка отправлена {_fmt(last)}, ждём одобрения админом",
            human=False,
            transient=True,
        )
    if state is not None and state.status == "manual":
        return JoinWhy(
            MANUAL,
            f"вручную: {err[:90] or 'нужен бот/капча'}",
            human=True,
            transient=False,
        )
    if spam_limited and not is_closed_chat(chat):
        until = f" (лимит до {spam_until[:16].replace('T', ' ')})" if spam_until else ""
        return JoinWhy(
            SPAMBLOCK,
            f"аккаунт в спамблоке — в публичный чат не вступить до снятия лимита{until}",
            human=False,
            transient=True,
        )
    if state is not None and (state.status == "abandoned" or state.fail_count >= max_attempts):
        nxt = (last + timedelta(hours=abandoned_retry_hours)) if last else None
        return JoinWhy(
            GAVE_UP,
            f"{state.fail_count} неудач подряд ({err[:80] or 'без текста'}); "
            f"следующая попытка {_fmt(nxt)}",
            human=False,
            transient=True,
        )
    if state is not None and state.fail_count > 0 and last is not None:
        wait = min(48.0, retry_hours * (2 ** (state.fail_count - 1)))
        nxt = last + timedelta(hours=wait)
        if nxt > moment:
            return JoinWhy(
                WAIT,
                f"попытка {state.fail_count}/{max_attempts} не вышла ({err[:80] or '—'}); "
                f"следующая {_fmt(nxt)}",
                human=False,
                transient=True,
            )
    return JoinWhy(
        READY,
        "ещё не вступал — автопилот вступит на ближайшем проходе",
        human=False,
        transient=True,
    )
