"""Советы по аккаунтам: польза против вреда, а не «доля недоступных чатов».

Для каждого аккаунта считаем два числа — и показываем их в сообщении:

польза = клиентов за 7 дн. · 5  +  отправок за 48 ч · 0,1 (не больше 60)  +  рабочих чатов · 0,5
вред   = банов · 2  +  повторов спамблока · 2  +  3, если ограничен сейчас  +  мутов · 0,5

«Лучше отключить» — только если клиентов нет, вред ≥ порога и вред больше пользы.
Спамблок при включённом sender — не повод отключать аккаунт: sender выключаем сами
(режим dead, остаётся schedule), аккаунт продолжает работать и приносить клиентов.
Причина «не в чате» всегда конкретная: бан / заявка ждёт / спамблок / нет ссылки / сдался.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.config import get_settings
from app.models import Account
from app.notify import safe_send
from app.operator.joinwhy import (
    BAN,
    GAVE_UP,
    MANUAL,
    NO_LINK,
    READY,
    REQUEST,
    SPAMBLOCK,
    WAIT,
    explain_not_member,
)
from app.store import Store
from app.utils.chat_ids import canon_chat_id
from app.utils.timefmt import fmt_until, parse_utc

log = logging.getLogger("marketing.advisor")

LEVEL_ORDER = {"stop": 0, "warn": 1, "info": 2, "ok": 3}

# что делает система по каждой причине «не в чате» (подпись в сообщении)
_PENDING_NOTE = {
    READY: "вступлю сама на ближайшем проходе",
    WAIT: "повторю попытку после паузы",
    REQUEST: "жду одобрения заявки админом",
    SPAMBLOCK: "вступлю сама, когда снимется спамблок",
    GAVE_UP: "повторю попытку позже (раз в несколько суток)",
    NO_LINK: "ЖДУ ССЫЛКУ: добавьте invite/@username в карточке чата",
    MANUAL: "НУЖНО ВРУЧНУЮ (капча/бот)",
}


def score_account(
    *,
    leads_7d: int,
    sent_48h: int,
    working: int,
    bans: int,
    soft_blocks: int,
    strikes: int,
    limited: bool,
) -> tuple[float, float]:
    """(польза, вред) — формулы в докстринге модуля."""
    value = leads_7d * 5.0 + min(sent_48h, 60) * 0.1 + working * 0.5
    harm = bans * 2.0 + strikes * 2.0 + (3.0 if limited else 0.0) + soft_blocks * 0.5
    return value, harm


def decide_level(
    *,
    value: float,
    harm: float,
    leads_7d: int,
    min_harm: float,
    limited: bool,
    handled: bool,
    pending_share: float,
    pending_human: bool,
    warn_share: float,
) -> str:
    """ok | info | warn | stop."""
    if harm >= min_harm and harm > value and leads_7d == 0:
        return "stop"
    if harm >= 2.0 and harm > value * 0.5 and not handled:
        return "warn"
    if pending_share >= warn_share and pending_human:
        return "warn"
    if handled or limited or pending_share >= warn_share:
        return "info"
    return "ok"


@dataclass
class AccountAdvice:
    account: Account
    level: str = "ok"  # ok | info | warn | stop
    total: int = 0
    works_in: list[str] = field(default_factory=list)
    blocked_in: list[str] = field(default_factory=list)  # бан / мут / спамблок пары
    pending_in: list[tuple[str, str, str]] = field(default_factory=list)  # (чат, код, причина)
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)  # что система сделала/сделает сама
    leads_7d: int = 0
    sent_48h: int = 0
    value: float = 0.0
    harm: float = 0.0

    @property
    def share(self) -> float:
        """Доля пар, где аккаунт не работает (бан/мут/не в чате)."""
        bad = len(self.blocked_in) + len(self.pending_in)
        return (bad / self.total) if self.total else 0.0

    def digest(self) -> str:
        raw = "|".join(
            [
                self.level,
                *sorted(self.works_in),
                "#",
                *sorted(self.blocked_in),
                "#",
                *sorted(f"{n}:{c}" for n, c, _t in self.pending_in),
                *self.reasons,
                *self.actions,
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _pending_lines(self) -> list[str]:
        groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for name, code, why in self.pending_in:
            groups[code].append((name, why))
        out: list[str] = []
        for code in (BAN, NO_LINK, MANUAL, GAVE_UP, SPAMBLOCK, REQUEST, WAIT, READY, ""):
            items = groups.get(code)
            if not items:
                continue
            note = _PENDING_NOTE.get(code, "")
            names = ", ".join(n for n, _w in items)
            detail = f" — {items[0][1]}" if code in {NO_LINK, MANUAL, GAVE_UP, WAIT} else ""
            out.append(f"Не в чате ({note}): {names}{detail}" if note else f"Не в чате: {names}")
        return out

    def text(self, mention: str = "") -> str:
        head = {
            "stop": "🛑 лучше ОТКЛЮЧИТЬ",
            "warn": "⚠ стоит проверить",
            "info": "ℹ под контролем",
        }.get(self.level, "ок")
        lines = [f"{mention} {head}: аккаунт {self.account.label}".strip()]
        if self.level == "stop":
            lines.append(
                f"Приносит больше вреда, чем пользы: польза {self.value:.1f} "
                f"(клиентов {self.leads_7d} за 7 дн., отправок {self.sent_48h} за 48 ч, "
                f"рабочих чатов {len(self.works_in)}) против вреда {self.harm:.1f}."
            )
        else:
            lines.append(
                f"Польза {self.value:.1f} (клиентов {self.leads_7d} за 7 дн., отправок "
                f"{self.sent_48h} за 48 ч, рабочих чатов {len(self.works_in)}) · "
                f"вред {self.harm:.1f}."
            )
        lines.extend(f"• {r}" for r in self.reasons)
        lines.extend(f"✅ {a}" for a in self.actions)
        if self.blocked_in:
            lines.append("Не отправляется: " + "; ".join(self.blocked_in))
        lines.extend(self._pending_lines())
        if self.works_in:
            lines.append("Отправляется только в: " + ", ".join(self.works_in))
        else:
            lines.append("Не отправляется ни в один чат.")
        return "\n".join(lines)


def will_demote(acc: Account) -> bool:
    """Спамблок + рабочий sender → sender пора выключать (dead)."""
    return bool(
        get_settings().spam_auto_dead
        and acc.is_spam_limited
        and not acc.is_dead
        and not acc.is_outreach
        and acc.has_sender
        and acc.sender_on
    )


async def demote_sender_on_spam(store: Store, acc: Account) -> bool:
    """Спамблок + рабочий sender: sender выключаем (dead), schedule остаётся.

    Спамблок чаще всего зарабатывает именно массовая рассылка sender; schedule при этом
    продолжает работать (особенно в закрытых чатах), так что аккаунт не списываем.
    """
    if not will_demote(acc):
        return False
    await store.update_account(acc.id, dead=1)
    return True


async def build_advice(store: Store, *, demoted: set[int] | None = None) -> list[AccountAdvice]:
    s = get_settings()
    demoted = demoted or set()
    chats = [c for c in await store.list_chats(enabled_only=True)]
    chat_by_pk = {c.id: c for c in chats}
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    states = {(x.account_id, x.chat_pk): x for x in await store.list_setup_states()}
    restr = {(r.account_id, r.chat_pk): r for r in await store.list_restrictions()}
    chat_bans = {(b.account_id, b.chat_pk): b for b in await store.list_bans(active_only=True)}
    join_states = {(j.account_id, j.chat_pk): j for j in await store.list_join_states()}
    disabled = await store.disabled_pairs()
    slots = {(x.account_id, x.chat_pk) for x in await store.all_slots()}
    scans = {(a, c): st for a, c, _t, st, _d in await store.list_scans()}
    now = datetime.now(timezone.utc)
    events = await store.send_events_between(
        (now - timedelta(hours=48)).isoformat(timespec="seconds"),
        (now + timedelta(minutes=1)).isoformat(timespec="seconds"),
    )
    sent_pairs = {(a, c) for a, c, _t in events}
    sent_by_acc: dict[int, int] = defaultdict(int)
    for a, _c, _t in events:
        sent_by_acc[a] += 1
    dms = await store.dm_summary(
        (now - timedelta(hours=24)).isoformat(timespec="seconds"),
        (now - timedelta(days=7)).isoformat(timespec="seconds"),
    )
    perfs = {p.account_id: p for p in await store.list_perf()}

    out: list[AccountAdvice] = []
    for acc in accounts:
        adv = AccountAdvice(account=acc)
        hard_bans = soft = 0
        pend_human = False
        for chat in chats:
            if (acc.id, canon_chat_id(chat.chat_id)) in disabled:
                continue  # выключено вручную — не считаем
            key = (acc.id, chat.id)
            name = chat.display_name
            r = restr.get(key)
            st = states.get(key)
            ban = chat_bans.get(key)
            if ban is not None or (r is not None and r.is_ban):
                adv.total += 1
                hard_bans += 1
                why = ""
                if ban is not None:
                    from app.jobs.bans import REASON_TEXT

                    why = REASON_TEXT.get(ban.reason, ban.reason or "")
                adv.blocked_in.append(f"{name} (бан{': ' + why if why else ''})")
            elif r is not None:
                adv.total += 1
                soft += 1
                if r.is_spamblock:
                    adv.blocked_in.append(f"{name} (пара закрыта спамблоком до {fmt_until(r.until_at)})")
                else:
                    adv.blocked_in.append(f"{name} (мут до {fmt_until(r.until_at)})")
            elif key in sent_pairs or (key in slots and (st is None or st.is_ok)):
                adv.total += 1
                adv.works_in.append(name)
            elif scans.get(key) == "not_member":
                adv.total += 1
                why = explain_not_member(
                    chat_by_pk[chat.id],
                    join_states.get(key),
                    spam_limited=acc.is_spam_limited,
                    spam_until=acc.spam_until,
                    retry_hours=s.autopilot_retry_hours,
                    max_attempts=s.autopilot_max_join_attempts,
                    abandoned_retry_hours=s.autopilot_abandoned_retry_hours,
                    now=now,
                )
                adv.pending_in.append((name, why.code, why.text))
                pend_human = pend_human or why.human
            elif st is not None and not st.is_ok:
                adv.total += 1
                adv.pending_in.append(
                    (name, WAIT, f"настройка не вышла: {(st.last_error or '—')[:70]}")
                )
            elif not chat.is_schedule:
                adv.total += 1
                adv.works_in.append(name)  # sender-чат: доступен через Autoposter
            # иначе пара ещё не настраивалась — не считаем ни плюсом, ни минусом

        spam_state = await store.get_spam_state(acc.id)
        d = dms.get(acc.id, {})
        perf = perfs.get(acc.id)
        adv.leads_7d = max(
            int(d.get("first_7d", 0) or 0),
            int(perf.wrote_n) if perf is not None and perf.measured else 0,
        )
        adv.sent_48h = sent_by_acc.get(acc.id, 0)
        adv.value, adv.harm = score_account(
            leads_7d=adv.leads_7d,
            sent_48h=adv.sent_48h,
            working=len(adv.works_in),
            bans=hard_bans,
            soft_blocks=soft,
            strikes=spam_state.strikes,
            limited=acc.is_spam_limited,
        )

        handled = False
        if acc.is_spam_limited:
            until = f" до {acc.spam_until}" if acc.spam_until else ""
            adv.reasons.append(f"@SpamBot: аккаунт ограничен{until}")
            if acc.id in demoted or acc.is_dead:
                handled = True
                adv.actions.append(
                    "sender выключен (режим dead): спамблок бьёт по рассылке sender, "
                    "schedule продолжает работать в доступных чатах"
                )
            elif will_demote(acc):
                handled = True
                adv.actions.append("при ближайшей проверке переведу в dead (sender выключу)")
            elif acc.is_outreach:
                adv.actions.append("аутрич-аккаунт: sender не используется, спамблок ожидаем")
        if hard_bans:
            adv.reasons.append(f"банов в чатах: {hard_bans}")
        if spam_state.strikes >= 2:
            adv.reasons.append(f"спамблок повторялся {spam_state.strikes} раз(а)")
        if adv.pending_in:
            adv.reasons.append(
                f"не в чате {len(adv.pending_in)} из {adv.total} "
                f"({len(adv.pending_in) / adv.total:.0%}) — причины ниже"
            )

        pending_share = (len(adv.pending_in) / adv.total) if adv.total else 0.0
        adv.level = decide_level(
            value=adv.value,
            harm=adv.harm,
            leads_7d=adv.leads_7d,
            min_harm=s.advisor_min_harm,
            limited=acc.is_spam_limited,
            handled=handled,
            pending_share=pending_share,
            pending_human=pend_human,
            warn_share=s.advisor_warn_share,
        )
        out.append(adv)
    out.sort(key=lambda a: (LEVEL_ORDER[a.level], a.account.label.casefold()))
    return out


async def run_advisor(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    force: bool = False,
) -> str:
    """Отправить советы по проблемным аккаунтам (повтор не чаще раза в сутки).

    Перед этим сами переводим в dead аккаунты со спамблоком и включённым sender.
    """
    s = get_settings()
    demoted: set[int] = set()
    for acc in await store.list_accounts():
        if acc.telethon_session and await demote_sender_on_spam(store, acc):
            demoted.add(acc.id)
            try:
                from app.jobs.spam import sync_sender_load

                await sync_sender_load(store, acc.id, None, None)
            except Exception:  # noqa: BLE001
                log.exception("advisor: не удалось остановить sender %s", acc.label)
    advice = [a for a in await build_advice(store, demoted=demoted) if a.level != "ok"]
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
    # @mention — на первом сообщении, где нужно решение (warn/stop); «info» — без пинга
    pinged = False
    blocks: list[str] = []
    for adv in fresh:
        mention = ""
        if not pinged and adv.level in {"warn", "stop"}:
            mention, pinged = s.advisor_mention, True
        blocks.append(adv.text(mention))
    text = "\n\n".join(blocks)
    if bot and admin_chat_id:
        await safe_send(bot, admin_chat_id, text)
    return text
