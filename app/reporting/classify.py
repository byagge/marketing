"""Классификация проблем аккаунта/чата по тексту ошибки и статусу."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProblemSpec:
    kind: str
    severity: str
    title_ru: str


# Порядок важен: более специфичные правила раньше.
_RULES: list[tuple[tuple[str, ...], ProblemSpec]] = [
    (
        ("userbannedinchannel", "you're banned", "banned from sending", "user is banned"),
        ProblemSpec("banned", "critical", "Забанили в чате"),
    ),
    (
        (
            "channelprivate",
            "chat_write_forbidden",
            "chatwriteforbidden",
            "kicked",
            "left the",
            "user_not_participant",
            "usernotparticipant",
            "not a participant",
            "you have left",
            "user deactivated",
        ),
        ProblemSpec("kicked", "critical", "Выкинули / нет доступа к чату"),
    ),
    (
        (
            "peerflood",
            "spamblock",
            "spam_block",
            "too many requests",
            "floodwait",
            "slowmodewait",
            "flood_wait",
            "userrestricted",
            "restricted",
        ),
        ProblemSpec("spam_block", "critical", "Спам-блок / Flood / ограничения"),
    ),
    (
        (
            "premium",
            "schedule_repeat",
            "only for premium",
            "premiumaccountrequired",
            "premium_account",
        ),
        ProblemSpec("premium_lost", "high", "Слетела Premium / нужен Premium"),
    ),
    (
        ("authkey", "session password", "authorization", "session revoked", "auth key"),
        ProblemSpec("session_dead", "critical", "Session мертва / отозвана"),
    ),
    (
        ("phone number banned", "banned phone", "user deactivated"),
        ProblemSpec("account_banned", "critical", "Аккаунт забанен Telegram"),
    ),
    (
        ("chatadminrequired", "admin required", "need admin"),
        ProblemSpec("no_rights", "high", "Нет прав писать"),
    ),
    (
        ("media", "photo", "forbidden", "allow_payment"),
        ProblemSpec("media_forbidden", "medium", "Медиа запрещено в чате"),
    ),
    (
        ("invite", "hash expired", "hash invalid", "invitehashexpired"),
        ProblemSpec("invite_broken", "high", "Инвайт-ссылка битая / истекла"),
    ),
    (
        ("username", "nobody is using", "username not occupied", "username invalid"),
        ProblemSpec("username_dead", "medium", "Username чата не резолвится"),
    ),
    (
        ("timeout", "connection", "network", "disconnected", "server closed"),
        ProblemSpec("network", "medium", "Сеть / соединение с Telegram"),
    ),
]


def classify_error(text: str) -> ProblemSpec | None:
    msg = (text or "").casefold()
    if not msg:
        return None
    compact = msg.replace(" ", "").replace("_", "")
    for markers, spec in _RULES:
        for m in markers:
            needle = m.casefold().replace(" ", "").replace("_", "")
            if needle in compact or m.casefold() in msg:
                return spec
    return None


def classify_or_unknown(text: str, *, default_title: str = "Неизвестная ошибка") -> ProblemSpec:
    """Любая ошибка → известный тип или «unknown», чтобы ничего не терять."""
    known = classify_error(text)
    if known:
        return known
    snippet = (text or "").strip()[:120] or default_title
    return ProblemSpec("unknown", "high", f"Прочее: {snippet[:60]}")


def classify_health_status(
    status: str,
    *,
    error: str = "",
    had_ok_before: bool = False,
) -> ProblemSpec | None:
    """Map health row → problem. Empty after prior OK is the loudest signal."""
    if error:
        known = classify_error(error)
        if known:
            return known
    if status == "error":
        if error:
            return classify_or_unknown(error, default_title="Ошибка проверки schedule")
        return ProblemSpec("schedule_error", "high", "Ошибка проверки schedule")
    if status == "empty":
        if had_ok_before:
            return ProblemSpec(
                "schedule_empty",
                "critical",
                "В schedule-чат не уходит — очередь пуста",
            )
        return ProblemSpec(
            "schedule_empty",
            "high",
            "Schedule не назначен (очередь пуста)",
        )
    if status == "low":
        return ProblemSpec("schedule_low", "medium", "Мало отложенных сообщений")
    return None


def severity_rank(severity: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(severity, 5)
