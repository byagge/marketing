"""Советы по аккаунтам: когда лучше отключить («больше вреда, чем пользы»)."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.config import get_settings
from app.models import Account
from app.notify import safe_send
from app.store import Store
from app.utils.chat_ids import canon_chat_id
from app.utils.timefmt import fmt_until, parse_utc

log = logging.getLogger("marketing.advisor")


@dataclass
class AccountAdvice:
    account: Account
    level: str  # ok | warn | stop
    total: int = 0
    works_in: list[str] = field(default_factory=list)
    blocked_in: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def share(self) -> float:
        return (len(self.blocked_in) / self.total) if self.total else 0.0

    def digest(self) -> str:
        raw = "|".join(
            [self.level, *sorted(self.works_in), "#", *sorted(self.blocked_in), *self.reasons]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def text(self, mention: str = "") -> str:
        head = {"stop": "🛑 лучше ОТКЛЮЧИТЬ", "warn": "⚠ стоит проверить"}.get(self.level, "ок")
        lines = [f"{mention} {head}: аккаунт {self.account.label}".strip()]
        if self.level == "stop":
            lines.append("Приносит больше вреда, чем пользы.")
        lines.extend(f"• {r}" for r in self.reasons)
        if self.blocked_in:
            lines.append("Не отправляется: " + "; ".join(self.blocked_in))
        if self.works_in:
            lines.append("Отправляется только в: " + ", ".join(self.works_in))
        else:
            lines.append("Не отправляется ни в один чат.")
        return "\n".join(lines)


async def build_advice(store: Store) -> list[AccountAdvice]:
    s = get_settings()
    chats = [c for c in await store.list_chats(enabled_only=True)]
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    states = {(x.account_id, x.chat_pk): x for x in await store.list_setup_states()}
    restr = {(r.account_id, r.chat_pk): r for r in await store.list_restrictions()}
    disabled = await store.disabled_pairs()
    slots = {(x.account_id, x.chat_pk) for x in await store.all_slots()}
    scans = {(a, c): st for a, c, _t, st, _d in await store.list_scans()}
    now = datetime.now(timezone.utc)
    events = await store.send_events_between(
        (now - timedelta(hours=48)).isoformat(timespec="seconds"),
        (now + timedelta(minutes=1)).isoformat(timespec="seconds"),
    )
    sent_pairs = {(a, c) for a, c, _t in events}

    out: list[AccountAdvice] = []
    for acc in accounts:
        adv = AccountAdvice(account=acc, level="ok")
        for chat in chats:
            if (acc.id, canon_chat_id(chat.chat_id)) in disabled:
                continue  # выключено вручную — не считаем
            key = (acc.id, chat.id)
            name = chat.display_name
            r = restr.get(key)
            st = states.get(key)
            if r is not None:
                adv.total += 1
                if r.is_ban:
                    adv.blocked_in.append(f"{name} (бан)")
                else:
                    adv.blocked_in.append(f"{name} (мут до {fmt_until(r.until_at)})")
            elif key in sent_pairs or (key in slots and (st is None or st.is_ok)):
                adv.total += 1
                adv.works_in.append(name)
            elif scans.get(key) == "not_member" or (st is not None and not st.is_ok):
                adv.total += 1
                adv.blocked_in.append(f"{name} (не вступлен/недоступен)")
            elif not chat.is_schedule:
                adv.total += 1
                adv.works_in.append(name)  # sender-чат: доступен через Autoposter
            # иначе пара ещё не настраивалась — не считаем ни плюсом, ни минусом

        if acc.is_spam_limited:
            until = f" до {acc.spam_until}" if acc.spam_until else ""
            adv.reasons.append(f"@SpamBot: аккаунт ограничен{until}")
        if adv.total and adv.share >= s.advisor_stop_share:
            adv.reasons.append(
                f"недоступно {len(adv.blocked_in)} из {adv.total} чатов "
                f"({adv.share:.0%})"
            )
        elif adv.total and adv.share >= s.advisor_warn_share:
            adv.reasons.append(
                f"недоступно {len(adv.blocked_in)} из {adv.total} чатов ({adv.share:.0%})"
            )

        if acc.is_spam_limited and adv.share >= s.advisor_warn_share:
            adv.level = "stop"
        elif adv.total and adv.share >= s.advisor_stop_share:
            adv.level = "stop"
        elif acc.is_spam_limited or (adv.total and adv.share >= s.advisor_warn_share):
            adv.level = "warn"
        out.append(adv)
    out.sort(key=lambda a: ({"stop": 0, "warn": 1, "ok": 2}[a.level], a.account.label.casefold()))
    return out


async def run_advisor(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    force: bool = False,
) -> str:
    """Отправить советы по проблемным аккаунтам (повтор не чаще раза в сутки)."""
    s = get_settings()
    advice = [a for a in await build_advice(store) if a.level != "ok"]
    if not advice:
        return "Советы: проблемных аккаунтов нет"
    fresh: list[AccountAdvice] = []
    for adv in advice:
        key = f"advisor:{adv.account.id}"
        last = await store.get_setting(key, "")
        digest, _, stamp = last.partition("|")
        when = parse_utc(stamp)
        recent = when is not None and (datetime.now(timezone.utc) - when) < timedelta(hours=24)
        if force or digest != adv.digest() or not recent:
            fresh.append(adv)
    if not fresh:
        return f"Советы: {len(advice)} проблемных аккаунтов, новых сообщений нет"
    for adv in fresh:
        await store.set_setting(
            f"advisor:{adv.account.id}",
            f"{adv.digest()}|{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        )
    text = "\n\n".join(a.text(s.advisor_mention if i == 0 else "") for i, a in enumerate(fresh))
    if bot and admin_chat_id:
        await safe_send(bot, admin_chat_id, text)
    return text
