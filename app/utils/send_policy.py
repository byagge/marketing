"""Правила отправки для пары аккаунт × чат: выключатели, баны, лимит постов, бэкофф вступлений."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Account, AccountChatPref, Chat, JoinState

MINUTES_PER_DAY = 24 * 60

# Причины, по которым пара не шлёт. Ключи используются и в диагностике.
REASON_PAIR_OFF = "pair_off"
REASON_BANNED = "banned"
REASON_CHAT_OFF = "chat_off"
REASON_SENDER_OFF = "sender_off"
REASON_DEAD = "dead"
REASON_SPAMBLOCK = "spamblock"
REASON_NO_POST = "no_post"

REASON_LABEL = {
    REASON_PAIR_OFF: "отключено вручную для этого аккаунта",
    REASON_BANNED: "бан в чате",
    REASON_CHAT_OFF: "чат выключен в каталоге",
    REASON_SENDER_OFF: "sender у аккаунта выключен",
    REASON_DEAD: "аккаунт в dead-режиме (sender выключен, только schedule)",
    REASON_SPAMBLOCK: "спамблок: sender остановлен на время блока",
    REASON_NO_POST: "в чат писать нельзя (стоп-лист)",
}


def matches_stoplist(title: str, keywords: tuple[str, ...] | list[str]) -> bool:
    """Название чата содержит слово из стоп-листа (регистр не важен)."""
    low = (title or "").casefold()
    return bool(low) and any(k and k.casefold() in low for k in keywords)


def is_no_post(chat: Chat, keywords: tuple[str, ...] | list[str] = ()) -> bool:
    """Чат помечен «писать нельзя» или попал в стоп-лист по названию."""
    if chat.no_post:
        return True
    return matches_stoplist(chat.title, keywords) or matches_stoplist(
        chat.display_name, keywords
    )


def is_send_allowed(
    account: Account,
    chat: Chat,
    pref: AccountChatPref | None,
    banned: bool,
    *,
    spam_load: int = 100,
    stoplist: tuple[str, ...] | list[str] = (),
) -> tuple[bool, str]:
    """
    Можно ли аккаунту слать в чат. Возвращает (ok, причина-ключ или "").

    - «писать нельзя» (флаг чата / стоп-лист), выключатель пары и бан — на schedule и sender;
    - dead, выключатель sender и спамблок (нагрузка 0%) — только на sender-чаты:
      schedule при спамблоке и dead остаётся как есть.
    """
    if not chat.enabled:
        return False, REASON_CHAT_OFF
    if is_no_post(chat, stoplist):
        return False, REASON_NO_POST
    if pref is not None and not pref.enabled:
        return False, REASON_PAIR_OFF
    if banned:
        return False, REASON_BANNED
    if chat.kind == "sender":
        if account.is_dead:
            return False, REASON_DEAD
        if not account.sender_on:
            return False, REASON_SENDER_OFF
        if spam_load <= 0:
            return False, REASON_SPAMBLOCK
    return True, ""


def effective_interval_minutes(chat: Chat) -> int:
    """Интервал чата с учётом лимита «не более N постов на аккаунт в сутки»."""
    base = max(1, int(chat.interval_minutes or 60))
    limit = int(chat.max_posts_per_account or 0)
    if limit <= 0:
        return base
    floor = -(-MINUTES_PER_DAY // limit)  # ceil
    return max(base, floor)


def effective_interval_seconds(chat: Chat) -> int:
    return effective_interval_minutes(chat) * 60


def pick_text_override(pref: AccountChatPref | None) -> str | None:
    if pref is not None and pref.has_text:
        return pref.text
    return None


_BAN_MARKERS = (
    "userbannedinchannel",
    "user_banned_in_channel",
    "you're banned",
    "you are banned",
    "you have been banned",
    "banned from",
    "kicked",
    "channel_banned",
    "channelbanned",
    "заблокирован",
    "забанен",
)
_FLOOD_MARKERS = ("floodwait", "flood_wait", "too many requests", "slowmode")
_BAD_INVITE_MARKERS = (
    "invitehashexpired",
    "invitehashinvalid",
    "инвайт недействителен",
    "invite link is",
    "expired",
)
_MUTE_MARKERS = ("chatwriteforbidden", "can't write", "cannot write", "write in this chat")


def classify_failure(text: str) -> str:
    """ban | flood | bad_invite | muted | other — по тексту ошибки."""
    msg = (text or "").casefold().replace(" ", "")
    raw = (text or "").casefold()
    if any(m.replace(" ", "") in msg for m in _FLOOD_MARKERS):
        return "flood"
    if any(m.replace(" ", "") in msg for m in _BAN_MARKERS):
        return "ban"
    if any(m.replace(" ", "") in msg for m in _BAD_INVITE_MARKERS):
        return "bad_invite"
    if any(m in raw for m in _MUTE_MARKERS):
        return "muted"
    return "other"


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def join_due(
    state: JoinState | None,
    *,
    now: datetime | None = None,
    retry_hours: float = 6.0,
    max_attempts: int = 5,
    request_wait_hours: float = 72.0,
) -> bool:
    """
    Пора ли пробовать вступать снова.

    - нет состояния → пора;
    - manual / abandoned → нет (до ручного сброса);
    - member → нет;
    - requested → через request_wait_hours;
    - pending → экспоненциальная пауза retry_hours · 2^(fails-1), максимум 48 ч.
    """
    if state is None:
        return True
    if state.status in {"manual", "abandoned", "member"}:
        return False
    moment = now or datetime.now(timezone.utc)
    last = _parse(state.last_attempt_at)
    if state.status == "requested":
        return last is None or moment - last >= timedelta(hours=request_wait_hours)
    if state.fail_count >= max_attempts:
        return False
    if last is None or state.fail_count <= 0:
        return True
    wait = min(48.0, retry_hours * (2 ** (state.fail_count - 1)))
    return moment - last >= timedelta(hours=wait)


# Сколько проверок подряд аккаунт должен «отсутствовать» в чате, где раньше был,
# чтобы считать это вылетом (одна проверка может быть ложной: сеть, FloodWait).
REMOVED_CONFIRM_MISSES = 2


def removal_confirmed(state: JoinState | None) -> bool:
    if state is None or not state.last_member_at:
        return False
    return state.miss_count >= REMOVED_CONFIRM_MISSES
