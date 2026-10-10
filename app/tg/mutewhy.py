"""Почему аккаунт получил мут / бан.

Что смотрим (только чтение, ничего не пишем в чат):
  1. сообщения в чате, адресованные аккаунту: отметка @username / по имени, ответ на его
     сообщение, фильтр «мои упоминания»;
  2. сообщения ботов-гарантов и модерации (антиспам, капча, WsGuard и т.п.) — даже без отметки;
  3. правила чата: описание и закреплённое сообщение;
  4. нашу собственную отправку из этого чата перед мутом (сколько раз, как давно).

Разбор делится на две части: сбор данных из Telegram (`gather`) и чистая функция `build_why`,
которая решает, что считать причиной. Вторая не зависит от Telethon и покрыта тестами.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import TelegramClient
from telethon.tl import functions, types

from app.utils.errfmt import short_error
from app.utils.tg_links import format_message_link

log = logging.getLogger("marketing.mutewhy")

CAUSE_LABEL: dict[str, str] = {
    "antispam": "антиспам чата: сообщения похожи на авторассылку / спам",
    "duplicate": "повтор одного и того же текста",
    "frequency": "слишком частая отправка",
    "links": "ссылки / упоминания запрещены",
    "ads": "реклама запрещена или не в том разделе",
    "verify": "не пройдена проверка (капча / подписка / верификация)",
    "rules": "нарушение правил чата",
    "admin": "решение администратора или модератора",
    "notice": "бот или админ написал аккаунту (формулировка ниже)",
    "pattern": "вероятная причина по нашей отправке",
    "not_found": "причина в чате не найдена",
    "unreadable": "чат не удалось прочитать",
    "chat_closed": "в чате писать нельзя никому",
    "account_spam": "ограничение самого аккаунта (@SpamBot)",
}

# порядок важен: первая подошедшая причина побеждает
_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "antispam",
        re.compile(
            r"автоматическ|автоматизир|авторассыл|рассылк|скрипт|сторонн\w*\s+бот|"
            r"\bspam|спам|automat|mass\s?send|массов",
            re.I,
        ),
    ),
    ("duplicate", re.compile(r"повтор|дублир|одинаков|duplicate|same message", re.I)),
    (
        "frequency",
        re.compile(
            r"слишком\s+част|не\s+чаще|раз\s+в\s+\d|кулдаун|cooldown|too\s+(often|fast|frequent)|"
            r"flood|флуд|интервал",
            re.I,
        ),
    ),
    ("links", re.compile(r"ссылк|\blinks?\b|\burl\b|t\.me/|упомин", re.I)),
    ("ads", re.compile(r"реклам|объявлен|прайс|advert|promo|раздел|топик", re.I)),
    (
        "verify",
        re.compile(
            r"капч|captcha|верифик|подтверд|не\s+прошл|подпис\w+\s+на\s+канал|subscribe|verify",
            re.I,
        ),
    ),
    # слова «правила / rules» сами по себе (шапка чата со ссылкой на правила) причиной не считаем —
    # нужно, чтобы речь шла о нарушении или запрете
    ("rules", re.compile(r"нарушен|violat|запрещ|forbidden|not allowed", re.I)),
    (
        "admin",
        re.compile(
            r"администратор|\bадмин|модератор|\badmin|moderator|замьют|замьюч|muted|banned|"
            r"забанен|ограничен|restricted|\bмут\b|в\s+мут|\bбан\b",
            re.I,
        ),
    ),
]

# причины, которые имеет смысл искать в правилах чата (описание / закреп)
_RULE_CAUSES = {"antispam", "duplicate", "frequency", "links", "ads", "rules"}
KNOWN_GUARD_BOTS = ("wsguardbot", "guard_lsa_bot", "lustifygarant_bot", "combot", "shieldy")
WINDOW_BEFORE = timedelta(hours=72)
WINDOW_AFTER = timedelta(minutes=20)


# сообщение бота, не адресованное аккаунту, считается объяснением только если оно про наказание
PUNISH_RX = re.compile(
    r"\bмут\b|в\s+мут|замьют|замьюч|ограничен|забанен|\bбан\b|кикнут|исключен|"
    r"\bmuted?\b|\bbanned\b|restricted|\bkicked\b",
    re.I,
)
_URL_RX = re.compile(r"https?://\S+|t\.me/\S+|telegra\.ph/\S+", re.I)


def classify_message(text: str) -> tuple[str, str] | None:
    """(причина, найденный фрагмент) или None."""
    raw = (text or "").strip()
    if not raw:
        return None
    for cause, rx in _RULES:
        m = rx.search(raw)
        if m:
            return cause, m.group(0)
    return None


@dataclass
class Ident:
    """Кто ищется в чате (аккаунт, который получил мут/бан)."""

    user_id: int = 0
    username: str = ""
    names: tuple[str, ...] = ()

    def mentioned_in(self, text: str) -> bool:
        low = (text or "").casefold()
        if not low:
            return False
        if self.username and f"@{self.username.casefold()}" in low:
            return True
        return any(n and len(n) >= 3 and n.casefold() in low for n in self.names)


@dataclass
class Msg:
    id: int
    text: str
    date: datetime | None = None
    sender: str = ""
    is_bot: bool = False
    to_me: bool = False
    link: str = ""


@dataclass
class Gathered:
    messages: list[Msg] = field(default_factory=list)
    rules: list[tuple[str, str, str]] = field(default_factory=list)  # (cause, фрагмент, источник)
    read_error: str = ""
    readable: bool = True


@dataclass
class Why:
    cause: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    link: str = ""
    certain: bool = False

    @property
    def evidence_text(self) -> str:
        return "\n".join(self.evidence)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _stamp(dt: datetime | None) -> str:
    dt = _utc(dt)
    return dt.strftime("%d.%m %H:%M UTC") if dt else "время не известно"


def _snip(text: str, limit: int = 260) -> str:
    one = re.sub(r"\s+", " ", (text or "").strip())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def describe_sends(sends: list[datetime], detected: datetime | None) -> str:
    """Короткая сводка по нашей отправке в этот чат перед мутом."""
    det = _utc(detected)
    if det is None:
        return ""
    before = sorted(s for s in (_utc(x) for x in sends) if s and s <= det)
    day = [s for s in before if s >= det - timedelta(hours=24)]
    if not day:
        return "в этот чат аккаунт за сутки до мута ничего не отправлял (по нашим фактам)"
    gap = int((det - day[-1]).total_seconds() // 60)
    gap_txt = f"{gap} мин" if gap < 180 else f"{gap // 60} ч"
    return (
        f"за сутки до мута аккаунт отправил в чат {len(day)} раз(а), "
        f"последняя отправка за {gap_txt} до обнаружения мута"
    )


def _in_window(m: Msg, detected: datetime | None) -> bool:
    det = _utc(detected)
    d = _utc(m.date)
    if det is None or d is None:
        return True
    return det - WINDOW_BEFORE <= d <= det + WINDOW_AFTER


def _score(m: Msg, cause: str | None, detected: datetime | None) -> float:
    score = 0.0
    if m.to_me:
        score += 3
    if m.is_bot:
        score += 2
    if cause:
        score += 1.5
    det, d = _utc(detected), _utc(m.date)
    if det and d:
        hours = abs((det - d).total_seconds()) / 3600
        score += max(0.0, 2.0 - hours / 12)  # свежее — важнее
    return score


def build_why(
    kind: str,
    *,
    gathered: Gathered,
    sends: list[datetime] | None = None,
    detected: datetime | None = None,
    until: datetime | None = None,
) -> Why:
    """Решить, что считать причиной, и собрать понятное объяснение."""
    sends = sends or []
    evidence: list[str] = []
    word = "бан" if kind == "ban" else "мут"

    ranked: list[tuple[float, Msg, str | None]] = []
    for m in gathered.messages:
        if not _in_window(m, detected):
            continue
        cls = classify_message(m.text)
        cause = cls[0] if cls else None
        if not (m.to_me or (m.is_bot and cause and PUNISH_RX.search(m.text))):
            continue
        ranked.append((_score(m, cause, detected), m, cause))
    ranked.sort(key=lambda t: t[0], reverse=True)

    for _, m, cause in ranked[:4]:
        who = m.sender or "?"
        flags = []
        if m.is_bot:
            flags.append("бот")
        if m.to_me:
            flags.append("адресовано аккаунту")
        flag = f" ({', '.join(flags)})" if flags else ""
        evidence.append(f"· {_stamp(m.date)} · {who}{flag}: «{_snip(m.text, 220)}»")
    if gathered.rules:
        for cause, frag, src in gathered.rules[:2]:
            evidence.append(f"· правила чата ({src}): «{_snip(frag, 200)}»")
    sends_line = describe_sends(sends, detected)
    if sends_line:
        evidence.append(f"· {sends_line}")
    if gathered.read_error:
        evidence.append(f"· чтение чата: {gathered.read_error}")

    if ranked:
        _, best, cause = ranked[0]
        direct = best.to_me and (best.is_bot or cause is not None)
        label = CAUSE_LABEL.get(cause or "notice")
        who = f"{best.sender or 'бот/админ'}"
        if cause:
            lead = "Причина" if direct else "Похоже, причина"
            summary = f"{lead}: {label}. {who} в {_stamp(best.date)}: «{_snip(best.text, 280)}»"
        else:
            summary = (
                f"{label[0].upper()}{label[1:]}. {who} в {_stamp(best.date)}: "
                f"«{_snip(best.text, 280)}»"
            )
        return Why(
            cause or "notice", summary, evidence, link=best.link, certain=direct and bool(cause)
        )

    if gathered.rules:
        cause, frag, src = gathered.rules[0]
        return Why(
            cause,
            f"Прямого сообщения о {word}е в чате не нашёл. В правилах чата ({src}) сказано: "
            f"«{_snip(frag, 220)}» — возможно, {word} связан с этим (это предположение).",
            evidence,
        )

    det = _utc(detected)
    last = None
    if det:
        done = [s for s in (_utc(x) for x in sends) if s and s <= det]
        last = max(done) if done else None
    if kind != "ban" and last is not None and (det - last) <= timedelta(minutes=45):
        gap = int((det - last).total_seconds() // 60)
        return Why(
            "pattern",
            f"Объяснения в чате не нашёл. Вероятно, антиспам среагировал на нашу отправку: "
            f"мут обнаружен через {gap} мин после последнего сообщения аккаунта "
            f"(это предположение, не подтверждено текстом из чата).",
            evidence,
        )

    if not gathered.readable:
        return Why(
            "unreadable",
            f"Чат не удалось прочитать ({gathered.read_error or 'нет доступа'}), "
            f"поэтому причину {word}а определить не получилось.",
            evidence,
        )
    extra = (
        "Бан мог быть выдан админом без объявления в чате."
        if kind == "ban"
        else "Мут могли выдать вручную админы или антиспам без публичного сообщения."
    )
    return Why("not_found", f"Причина {word}а в чате не найдена. {extra}", evidence)


# ---- сбор данных из Telegram ---------------------------------------------------------------


def _chat_link(entity: Any, msg_id: int) -> str:
    username = (getattr(entity, "username", None) or "").strip()
    try:
        if username:
            return format_message_link(username, msg_id)
        return format_message_link(int(f"-100{entity.id}"), msg_id)
    except Exception:  # noqa: BLE001
        return ""


def _name_of(sender: Any) -> str:
    if sender is None:
        return ""
    username = (getattr(sender, "username", None) or "").strip()
    if username:
        return f"@{username}"
    first = (getattr(sender, "first_name", None) or "").strip()
    title = (getattr(sender, "title", None) or "").strip()
    return first or title


def _is_guard(sender: Any, extra_bots: set[str]) -> bool:
    if sender is None:
        return False
    if getattr(sender, "bot", False):
        return True
    uname = (getattr(sender, "username", None) or "").casefold()
    return bool(uname) and (uname in extra_bots or uname in KNOWN_GUARD_BOTS)


def _mentions_user_id(msg: Any, user_id: int) -> bool:
    if not user_id:
        return False
    for ent in getattr(msg, "entities", None) or []:
        if isinstance(ent, types.MessageEntityMentionName) and ent.user_id == user_id:
            return True
    return False


async def _own_ids(client: TelegramClient, entity: Any, who: Any, limit: int = 25) -> set[int]:
    ids: set[int] = set()
    try:
        async for m in client.iter_messages(entity, limit=limit, from_user=who):
            ids.add(int(m.id))
    except Exception as e:  # noqa: BLE001
        log.debug("own messages lookup failed: %s", e)
    return ids


async def gather(
    client: TelegramClient,
    entity: Any,
    ident: Ident,
    *,
    self_reader: bool,
    extra_bots: set[str] | None = None,
    limit: int = 90,
) -> Gathered:
    """Прочитать чат глазами аккаунта (self_reader) или другого нашего аккаунта."""
    out = Gathered()
    bots = {b.casefold().lstrip("@") for b in (extra_bots or set()) if b}

    mention_ids: set[int] = set()
    if self_reader:
        try:
            async for m in client.iter_messages(
                entity, limit=20, filter=types.InputMessagesFilterMyMentions()
            ):
                mention_ids.add(int(m.id))
        except Exception as e:  # noqa: BLE001
            log.debug("my mentions lookup failed: %s", e)
    who: Any = "me" if self_reader else (ident.user_id or ident.username or None)
    own = await _own_ids(client, entity, who) if who else set()

    seen: set[int] = set()
    try:
        async for m in client.iter_messages(entity, limit=limit):
            seen.add(int(m.id))
            await _consider(client, entity, m, ident, own, mention_ids, bots, out)
    except Exception as e:  # noqa: BLE001
        out.read_error = short_error(e)
        out.readable = False
    # отметки, которые не попали в последние `limit` сообщений
    for mid in mention_ids - seen:
        try:
            m = await client.get_messages(entity, ids=mid)
        except Exception:  # noqa: BLE001
            continue
        if m is not None:
            await _consider(client, entity, m, ident, own, mention_ids, bots, out)

    if out.readable or out.messages:
        out.readable = True
        await _read_rules(client, entity, out)
    return out


async def _consider(
    client: TelegramClient,
    entity: Any,
    m: Any,
    ident: Ident,
    own: set[int],
    mention_ids: set[int],
    bots: set[str],
    out: Gathered,
) -> None:
    text = (getattr(m, "message", "") or "").strip()
    if not text:
        return
    if getattr(m, "out", False):
        return  # сообщения самого читающего аккаунта — не объяснение
    reply_to = getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None)
    to_me = (
        int(m.id) in mention_ids
        or ident.mentioned_in(text)
        or _mentions_user_id(m, ident.user_id)
        or (reply_to is not None and int(reply_to) in own)
    )
    cls = classify_message(text)
    if not (to_me or cls):
        return
    sender = None
    try:
        sender = await m.get_sender()
    except Exception:  # noqa: BLE001
        pass
    if sender is not None and ident.user_id and getattr(sender, "id", None) == ident.user_id:
        return  # собственные сообщения аккаунта — не объяснение
    is_bot = _is_guard(sender, bots)
    if not (to_me or is_bot):
        return
    if not to_me and not PUNISH_RX.search(text):
        return  # шапка чата, приветствие и т.п. — не про наказание
    out.messages.append(
        Msg(
            id=int(m.id),
            text=text,
            date=getattr(m, "date", None),
            sender=_name_of(sender),
            is_bot=is_bot,
            to_me=to_me,
            link=_chat_link(entity, int(m.id)),
        )
    )


async def _read_rules(client: TelegramClient, entity: Any, out: Gathered) -> None:
    """Правила: описание чата и закреплённое сообщение."""
    texts: list[tuple[str, str]] = []
    try:
        if isinstance(entity, types.Channel):
            full = await client(functions.channels.GetFullChannelRequest(entity))
            about = (getattr(full.full_chat, "about", "") or "").strip()
            if about:
                texts.append(("описание", about))
    except Exception as e:  # noqa: BLE001
        log.debug("full channel failed: %s", e)
    try:
        async for m in client.iter_messages(
            entity, limit=1, filter=types.InputMessagesFilterPinned()
        ):
            pinned = (getattr(m, "message", "") or "").strip()
            if pinned:
                texts.append(("закреп", pinned))
    except Exception as e:  # noqa: BLE001
        log.debug("pinned failed: %s", e)
    for src, text in texts:
        for line in re.split(r"[\n\r]+|(?<=[.!?])\s+", text):
            bare = _URL_RX.sub("", line).strip()
            if len(bare) < 20:  # «Rules - ссылка» и т.п. — самих правил здесь нет
                continue
            cls = classify_message(bare)
            if cls and cls[0] in _RULE_CAUSES:
                out.rules.append((cls[0], bare, src))
                break  # по одному найденному правилу с источника достаточно
