"""Диагностика пары «аккаунт × чат»: причина → действие → нужен ли человек.

Чистая логика без Telegram и БД: на вход — факты, на выход — решение.
Порядок правил важен: сначала то, что объясняет молчание, потом — что чинить.
"""

from __future__ import annotations

from dataclasses import dataclass

# причины
WORKING = "working"
WARMING = "warming"  # настроено недавно, рано судить
DISABLED = "disabled"
BANNED = "banned"
MUTED = "muted"
NO_DATA = "no_data"
NOT_MEMBER = "not_member"
NOT_MEMBER_NO_LINK = "not_member_no_link"
NOT_SCHEDULED = "not_scheduled"
SILENT = "silent"
ACCOUNT_SPAM = "account_spam"
SESSION_DEAD = "session_dead"
SENDER = "sender"  # sender-чат: по плану не оцениваем

# действия
JOIN = "join"
SETUP = "setup"
RESETUP = "resetup"
COLLECT = "collect"

STALE_SCAN_MIN = 150  # данные старше — считаем «нет данных», а не «не отправляет»


@dataclass
class PairFacts:
    account_id: int
    account_label: str
    chat_pk: int
    chat_title: str
    chat_kind: str = "schedule"
    interval: int = 60
    pref_enabled: bool = True
    restriction: str | None = None  # ban | mute | nowrite
    restriction_until: str = ""
    has_slot: bool = False
    setup_status: str | None = None  # ok | pending | abandoned
    setup_error: str = ""
    setup_age_min: float | None = None  # сколько минут назад последний setup
    scan_status: str | None = None  # ok | not_member | error
    scan_age_min: float | None = None
    sends_3h: int = 0
    sends_24h: int = 0
    account_spam: str = ""  # clean | limited | unknown | ""
    has_join_method: bool = True
    session_error: str = ""


@dataclass
class Diagnosis:
    cause: str
    severity: str  # critical | high | medium | low | info
    action: str | None = None  # что оператор может сделать сам
    human: bool = False  # нужен человек
    text: str = ""  # что случилось (по-русски, конкретно)
    todo: str = ""  # что сделать человеку

    @property
    def is_problem(self) -> bool:
        return self.cause not in {WORKING, WARMING, DISABLED, SENDER, NO_DATA}


def warmup_minutes(interval: int) -> float:
    """Сколько ждать после настройки, прежде чем считать молчание проблемой."""
    return max(1, int(interval)) + 45.0


def diagnose_pair(f: PairFacts) -> Diagnosis:
    if not f.pref_enabled:
        return Diagnosis(DISABLED, "info", text="выключено вручную")
    if f.chat_kind != "schedule":
        return Diagnosis(SENDER, "info", text="sender-чат")

    if f.session_error:
        return Diagnosis(
            SESSION_DEAD,
            "critical",
            human=True,
            text=f"сессия не читается: {f.session_error[:120]}",
            todo="перевыпустить .session для аккаунта (кнопка «Session»)",
        )
    if f.restriction == "ban":
        return Diagnosis(
            BANNED,
            "high",
            text="аккаунт забанен в чате",
            todo="снять бан у админов чата или убрать аккаунт из чата",
        )
    if f.restriction in {"mute", "nowrite"}:
        until = f" до {f.restriction_until}" if f.restriction_until else ""
        return Diagnosis(MUTED, "medium", text=f"мут / нельзя писать{until}")

    stale = f.scan_age_min is None or f.scan_age_min > STALE_SCAN_MIN or f.scan_status == "error"
    if stale:
        return Diagnosis(
            NO_DATA,
            "low",
            action=COLLECT,
            text="нет свежих данных по этому аккаунту (сбор не удался или давно не было)",
        )

    if f.scan_status == "not_member":
        if f.has_join_method:
            return Diagnosis(
                NOT_MEMBER,
                "medium",
                action=JOIN,
                text="аккаунт не состоит в чате — вступаю",
            )
        return Diagnosis(
            NOT_MEMBER_NO_LINK,
            "high",
            human=True,
            text="аккаунт не в чате, а ссылки/способа вступления нет",
            todo="добавьте invite-ссылку или @username в карточке чата",
        )

    # аккаунт в чате
    if f.account_spam == "limited" and f.sends_3h == 0:
        return Diagnosis(
            ACCOUNT_SPAM,
            "high",
            human=True,
            text="@SpamBot: у аккаунта ограничение — писать в группы нельзя",
            todo="подождать окончания ограничения или заменить аккаунт",
        )
    if not f.has_slot or f.setup_status != "ok":
        err = f" ({f.setup_error[:80]})" if f.setup_error else ""
        return Diagnosis(
            NOT_SCHEDULED,
            "high",
            action=SETUP,
            text=f"аккаунт в чате, но отправка не настроена{err} — настраиваю",
        )
    if f.sends_3h > 0:
        return Diagnosis(WORKING, "info", text="отправляет")
    if f.setup_age_min is not None and f.setup_age_min < warmup_minutes(f.interval):
        return Diagnosis(WARMING, "info", text="настроено недавно, ждём первую отправку")
    return Diagnosis(
        SILENT,
        "high",
        action=RESETUP,
        text="настроено и аккаунт в чате, но за 3 часа ни одного сообщения — пересоздаю очередь",
        todo="если и после пересоздания не пишет: проверьте чат вручную "
        "(модерация удаляет сообщения / антиспам-бот)",
    )
