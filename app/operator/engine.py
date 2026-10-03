"""Цикл ИИ-оператора: факты → диагноз → безопасные действия → то, что нужно человеку.

Принципы:
* Только факты. Решения принимаются по истории чатов (кто реально писал), членству,
  банам/мутам и SpamBot — не по плану и не по статусу очереди.
* Чиним сами только безопасное (вступить, настроить, пересоздать очередь) и с ограничениями:
  кулдаун и лимит попыток на пару в сутки — никаких бесконечных повторов.
* Человеку — только то, что без него не решить, сгруппировано и с конкретным действием.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import Bot

from app.config import get_settings
from app.jobs import runtime
from app.operator.diagnose import (
    ACCOUNT_SPAM,
    SPAMBLOCKED,
    COLLECT,
    JOIN,
    NOT_MEMBER,
    NOT_MEMBER_NO_LINK,
    NOT_SCHEDULED,
    RESETUP,
    SESSION_DEAD,
    SETUP,
    SILENT,
    Diagnosis,
    PairFacts,
    diagnose_pair,
)
from app.operator.needs import ChatNeed, accounts_to_buy, compute_needs
from app.store import Store
from app.utils.chat_ids import canon_chat_id
from app.utils.timefmt import parse_utc, to_iso

log = logging.getLogger("marketing.operator")

# кулдаун (ч) и максимум попыток в сутки на пару для каждого действия
POLICY: dict[str, tuple[float, int]] = {
    JOIN: (6.0, 2),
    SETUP: (3.0, 3),
    RESETUP: (3.0, 2),
}
MAX_ACCOUNTS_PER_CYCLE = 8
MAX_PAIRS_PER_ACCOUNT = 6
SPAMBLOCK_PROBES = 3  # проб новых чатов за цикл у аккаунта с лимитом SpamBot
RECENT_KEY = "op:recent"
SESSION_ERR_MARKERS = ("не авториз", "unauthorized", "authkey", "session", "revoked", "deactivated")


@dataclass
class ActionResult:
    action: str
    account_id: int
    account_label: str
    chat_pks: list[int]
    ok: bool
    text: str


@dataclass
class Escalation:
    key: str
    severity: str
    title: str
    todo: str


@dataclass
class CycleResult:
    pairs: int = 0
    by_cause: dict[str, int] = field(default_factory=dict)
    actions: list[ActionResult] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    diagnoses: list[tuple[PairFacts, Diagnosis]] = field(default_factory=list)
    skipped_reason: str = ""
    needs: list[ChatNeed] = field(default_factory=list)

    @property
    def working(self) -> int:
        return self.by_cause.get("working", 0)


# ------------------------------------------------------------------ сбор фактов

def _has_join_method(chat) -> bool:
    from app.tg.join import chat_join_target

    return chat_join_target(chat)["method"] in {"garant", "folder", "invite", "username"}


async def gather_pairs(store: Store, now: datetime | None = None) -> list[PairFacts]:
    now = now or datetime.now(timezone.utc)
    chats = [c for c in await store.list_chats(enabled_only=True)]
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    slots = {(s.account_id, s.chat_pk) for s in await store.all_slots()}
    states = {(s.account_id, s.chat_pk): s for s in await store.list_setup_states()}
    restr = {(r.account_id, r.chat_pk): r for r in await store.list_restrictions()}
    disabled = await store.disabled_pairs()
    scans = {(a, c): (parse_utc(t), st) for a, c, t, st, _d in await store.list_scans()}
    events = await store.send_events_between(
        to_iso(now - timedelta(hours=24)), to_iso(now + timedelta(minutes=1))
    )
    h3 = now - timedelta(hours=3)
    s3: dict[tuple[int, int], int] = defaultdict(int)
    s24: dict[tuple[int, int], int] = defaultdict(int)
    for a, c, t in events:
        s24[(a, c)] += 1
        dt = parse_utc(t)
        if dt and dt >= h3:
            s3[(a, c)] += 1

    session_err: dict[int, str] = {}
    for acc in accounts:
        raw = await store.get_setting(f"facts_status:{acc.id}", "")
        st, _, rest = raw.partition("|")
        _at, _, err = rest.partition("|")
        if st == "err" and any(m in err.casefold() for m in SESSION_ERR_MARKERS):
            session_err[acc.id] = err

    join_ok = {c.id: _has_join_method(c) for c in chats}
    now_iso = to_iso(now)
    out: list[PairFacts] = []
    for acc in accounts:
        for chat in chats:
            key = (acc.id, chat.id)
            st = states.get(key)
            r = restr.get(key)
            scan = scans.get(key)
            scan_age = (
                (now - scan[0]).total_seconds() / 60 if scan and scan[0] is not None else None
            )
            setup_age = None
            if st is not None and st.is_ok:
                at = parse_utc(st.last_attempt_at)
                if at is not None:
                    setup_age = (now - at).total_seconds() / 60
            out.append(
                PairFacts(
                    account_id=acc.id,
                    account_label=acc.label,
                    chat_pk=chat.id,
                    chat_title=chat.display_name,
                    chat_kind=chat.kind,
                    interval=chat.interval_minutes,
                    pref_enabled=(acc.id, canon_chat_id(chat.chat_id)) not in disabled,
                    restriction=r.kind if r else None,
                    restriction_until=(r.until_at[:16].replace("T", " ") if r and r.until_at else ""),
                    restriction_expired=bool(r and r.until_at and r.until_at <= now_iso),
                    is_private=not (chat.username or "").strip(),
                    has_slot=key in slots,
                    setup_status=st.status if st else None,
                    setup_error=st.last_error if st else "",
                    setup_age_min=setup_age,
                    scan_status=scan[1] if scan else None,
                    scan_age_min=scan_age,
                    sends_3h=s3.get(key, 0),
                    sends_24h=s24.get(key, 0),
                    account_spam=acc.spam_status,
                    has_join_method=join_ok.get(chat.id, False),
                    session_error=session_err.get(acc.id, ""),
                )
            )
    return out


# ------------------------------------------------------------ кулдаун / лимиты

async def _attempt_state(store: Store, action: str, acc: int, chat: int, now: datetime) -> tuple[datetime | None, int]:
    raw = await store.get_setting(f"op:{action}:{acc}:{chat}", "")
    ts, _, rest = raw.partition("|")
    day, _, n = rest.partition("|")
    last = parse_utc(ts)
    today = now.date().isoformat()
    count = int(n) if (n.isdigit() and day == today) else 0
    return last, count


async def _record_attempt(store: Store, action: str, acc: int, chat: int, now: datetime) -> None:
    _last, n = await _attempt_state(store, action, acc, chat, now)
    await store.set_setting(
        f"op:{action}:{acc}:{chat}", f"{to_iso(now)}|{now.date().isoformat()}|{n + 1}"
    )


async def can_act(store: Store, action: str, acc: int, chat: int, now: datetime) -> tuple[bool, bool]:
    """(можно ли сейчас, исчерпаны ли попытки за сутки)."""
    cooldown_h, max_n = POLICY[action]
    last, n = await _attempt_state(store, action, acc, chat, now)
    if n >= max_n:
        return False, True
    if last is not None and (now - last) < timedelta(hours=cooldown_h):
        return False, False
    return True, False


# ---------------------------------------------------------------- исполнители

async def _exec_join(store: Store, account_id: int, chat_pks: list[int]) -> tuple[bool, str]:
    from app.jobs.join import run_join_chats

    results = await run_join_chats(
        store, account_id, chat_pks, None, None, job_kind="join_chats", notify_each=False
    )
    good = [r for r in results if r.status in {"joined", "already", "captcha_ok"}]
    req = [r for r in results if r.status == "request_sent"]
    bad = [f"{r.chat_title}: {r.status}" + (f" ({r.detail[:60]})" if r.detail else "")
           for r in results if r.status not in {"joined", "already", "captcha_ok", "request_sent"}]
    parts = []
    if good:
        parts.append(f"вступил: {', '.join(r.chat_title for r in good)}")
    if req:
        parts.append(f"заявка отправлена: {', '.join(r.chat_title for r in req)}")
    if bad:
        parts.append("не вышло — " + "; ".join(bad))
    return bool(good or req), "; ".join(parts) or "нет результата"


async def _exec_setup(store: Store, account_id: int, chat_pks: list[int]) -> tuple[bool, str]:
    from app.jobs.setup import run_setup_chats_only

    for pk in chat_pks:
        await store.reset_setup_state(account_id, pk)
    buckets = await run_setup_chats_only(
        store, account_id, chat_pks, None, None, job_kind="setup_retry", skip_abandoned=False
    )
    ok = len(buckets.get("ok") or [])
    bad = [t for k in ("skipped", "abandoned", "config_error") for t in buckets.get(k, [])]
    msg = f"настроено {ok}/{len(chat_pks)}"
    if bad:
        msg += f"; не вышло: {', '.join(bad[:3])}"
    return ok > 0, msg


async def _exec_collect(store: Store, account_id: int, chat_pks: list[int]) -> tuple[bool, str]:
    from app.jobs.facts import run_facts_collection

    out = await run_facts_collection(
        store, hours=3, scan_restrictions=True, scan_dms=False, account_ids=[account_id], quiet=True
    )
    return True, out[:120]


Executor = Callable[[Store, int, list[int]], Awaitable[tuple[bool, str]]]
EXECUTORS: dict[str, Executor] = {
    JOIN: _exec_join,
    SETUP: _exec_setup,
    RESETUP: _exec_setup,
    COLLECT: _exec_collect,
}


# ---------------------------------------------------------------------- цикл

def _escalations(
    diagnoses: list[tuple[PairFacts, Diagnosis]], exhausted: set[tuple[str, int, int]]
) -> list[Escalation]:
    """Свести проблемы в короткий список «что нужно от человека»."""
    out: list[Escalation] = []
    by_chat: dict[tuple[str, int], list[PairFacts]] = defaultdict(list)
    for f, d in diagnoses:
        if d.cause == NOT_MEMBER_NO_LINK:
            by_chat[(d.cause, f.chat_pk)].append(f)
        elif d.cause in {SILENT, NOT_SCHEDULED, NOT_MEMBER}:
            act = {SILENT: RESETUP, NOT_SCHEDULED: SETUP, NOT_MEMBER: JOIN}[d.cause]
            if (act, f.account_id, f.chat_pk) in exhausted:
                by_chat[(d.cause + ":exhausted", f.chat_pk)].append(f)
        elif d.cause in {ACCOUNT_SPAM, SESSION_DEAD}:
            out.append(
                Escalation(
                    key=f"{d.cause}:{f.account_id}",
                    severity=d.severity,
                    title=f"{f.account_label}: {d.text}",
                    todo=d.todo,
                )
            )
    seen_acc: set[str] = set()
    out = [e for e in out if not (e.key in seen_acc or seen_acc.add(e.key))]
    for (cause, chat_pk), items in by_chat.items():
        title = items[0].chat_title
        names = ", ".join(sorted({i.account_label for i in items})[:8])
        more = f" +{len({i.account_label for i in items}) - 8}" if len({i.account_label for i in items}) > 8 else ""
        if cause == NOT_MEMBER_NO_LINK:
            out.append(
                Escalation(
                    key=f"{cause}:{chat_pk}",
                    severity="high",
                    title=f"«{title}»: {len(items)} акк. не в чате, способа вступить нет ({names}{more})",
                    todo="добавьте invite-ссылку или @username в карточке чата — вступят сами",
                )
            )
        elif cause.startswith(SILENT):
            out.append(
                Escalation(
                    key=f"{cause}:{chat_pk}",
                    severity="high",
                    title=f"«{title}»: {len(items)} акк. в чате и настроены, но сообщений нет даже после пересоздания ({names}{more})",
                    todo="проверьте чат вручную: модерация удаляет сообщения или антиспам-бот; "
                    "если так — выключите эти аккаунты в чате («Чаты аккаунта»)",
                )
            )
        elif cause.startswith(NOT_SCHEDULED):
            err = next((i.setup_error for i in items if i.setup_error), "")
            out.append(
                Escalation(
                    key=f"{cause}:{chat_pk}",
                    severity="high",
                    title=f"«{title}»: не удаётся настроить отправку у {len(items)} акк. ({names}{more})"
                    + (f" — {err[:80]}" if err else ""),
                    todo="посмотрите причину в «Баны / муты» → аккаунт; возможно нужен мут/бан-разбор",
                )
            )
        elif cause.startswith(NOT_MEMBER):
            out.append(
                Escalation(
                    key=f"{cause}:{chat_pk}",
                    severity="high",
                    title=f"«{title}»: автовступление не удалось у {len(items)} акк. ({names}{more})",
                    todo="нужна капча/заявка вручную или другая ссылка вступления",
                )
            )
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    out.sort(key=lambda e: (order.get(e.severity, 5), e.title))
    return out


def parse_spam_until_safe(f: PairFacts) -> datetime:
    """Когда пробовать снова: не раньше чем через сутки."""
    return datetime.now(timezone.utc) + timedelta(hours=24)


def _spam_escalations(diagnoses: list[tuple[PairFacts, Diagnosis]]) -> list[Escalation]:
    """Аккаунт с лимитом SpamBot, который не пишет ни в один чат."""
    per: dict[int, dict[str, Any]] = {}
    for f, d in diagnoses:
        if f.chat_kind != "schedule":
            continue
        row = per.setdefault(
            f.account_id,
            {"label": f.account_label, "limited": f.account_spam == "limited", "work": 0, "blocked": 0},
        )
        if d.cause == "working":
            row["work"] += 1
        elif d.cause == SPAMBLOCKED:
            row["blocked"] += 1
    out = []
    for acc_id, row in per.items():
        if row["limited"] and row["work"] == 0 and row["blocked"] >= 2:
            out.append(
                Escalation(
                    key=f"{ACCOUNT_SPAM}:{acc_id}",
                    severity="high",
                    title=f"{row['label']}: SpamBlock — не отправляет ни в один чат "
                    f"(закрыто {row['blocked']}, остальные проверены)",
                    todo="подождать окончания лимита (проверяю @SpamBot каждые 3 ч) или заменить аккаунт",
                )
            )
    return out


def _need_escalation(needs: list[ChatNeed]) -> Escalation | None:
    buy = accounts_to_buy(needs)
    short = [n for n in needs if n.deficit > 0]
    if not buy or not short:
        return None
    lines = ", ".join(
        f"{n.title}: есть {n.eligible} из {n.needed}" for n in short[:6]
    )
    more = f" и ещё {len(short) - 6} чатов" if len(short) > 6 else ""
    prio = any(n.priority for n in short)
    return Escalation(
        key="need_accounts",
        severity="high" if prio else "medium",
        title=f"Нужно ещё ≈{buy} новых аккаунтов, чтобы в чатах писали не реже раза в 5 минут. "
        f"Сейчас с учётом всего, что починю сама: {lines}{more}",
        todo=f"добавьте ≈{buy} чистых аккаунтов (кнопка «Session», пост задать) — "
        f"сама вступлю во все чаты и настрою",
    )


async def _remember(store: Store, results: list[ActionResult], now: datetime) -> None:
    if not results:
        return
    raw = await store.get_setting(RECENT_KEY, "[]")
    try:
        items = json.loads(raw)
    except Exception:  # noqa: BLE001
        items = []
    for r in results:
        items.append({"at": to_iso(now), "ok": r.ok, "text": f"{r.account_label}: {r.text}"})
    await store.set_setting(RECENT_KEY, json.dumps(items[-40:], ensure_ascii=False))


async def recent_actions(store: Store, hours: float = 24.0) -> list[dict[str, Any]]:
    try:
        items = json.loads(await store.get_setting(RECENT_KEY, "[]"))
    except Exception:  # noqa: BLE001
        return []
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [i for i in items if (parse_utc(i.get("at", "")) or cut) >= cut]


async def sync_incidents(store: Store, result: CycleResult) -> None:
    """Открыть/закрыть инциденты по актуальной диагностике (баны и муты — в своих таблицах)."""
    keep: set[tuple[str, int | None, int | None]] = set()
    per_chat: dict[tuple[str, int], list[PairFacts]] = defaultdict(list)
    for f, d in result.diagnoses:
        if not d.is_problem:
            continue
        if d.cause in {ACCOUNT_SPAM, SESSION_DEAD}:
            k = (d.cause, f.account_id, None)
            if k not in keep:
                keep.add(k)
                await store.upsert_incident(
                    kind=d.cause,
                    severity=d.severity,
                    title=d.text,
                    detail=d.todo,
                    account_id=f.account_id,
                    source="operator",
                )
        else:
            per_chat[(d.cause, f.chat_pk)].append(f)
    cause_title = {
        NOT_MEMBER: "аккаунты не в чате (вступаю сам)",
        NOT_MEMBER_NO_LINK: "аккаунты не в чате, способа вступить нет",
        NOT_SCHEDULED: "аккаунты в чате, но отправка не настроена (настраиваю сам)",
        SILENT: "настроено, но сообщений нет",
    }
    for (cause, chat_pk), items in per_chat.items():
        keep.add((cause, None, chat_pk))
        names = ", ".join(sorted({i.account_label for i in items}))
        sev = "high" if cause in {NOT_MEMBER_NO_LINK, SILENT, NOT_SCHEDULED} else "medium"
        await store.upsert_incident(
            kind=cause,
            severity=sev,
            title=f"«{items[0].chat_title}»: {cause_title.get(cause, cause)} — {len({i.account_label for i in items})} акк.",
            detail=names[:300],
            chat_pk=chat_pk,
            source="operator",
        )
    await store.resolve_incidents_not_in("operator", keep)


async def run_cycle(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    act: bool = True,
) -> CycleResult:
    now = datetime.now(timezone.utc)
    result = CycleResult()
    # старые «выдуманные» проблемы прежнего маркетолога закрываем один раз
    if await store.get_setting("op:legacy_closed", "") != "1":
        await store.resolve_incidents_by_source("marketer")
        await store.set_setting("op:legacy_closed", "1")

    pairs = await gather_pairs(store, now)
    result.pairs = len(pairs)
    for f in pairs:
        d = diagnose_pair(f)
        result.diagnoses.append((f, d))
        result.by_cause[d.cause] = result.by_cause.get(d.cause, 0) + 1

    auto_fix = (await store.get_setting("marketer_auto_fix", "1")) == "1"
    exhausted: set[tuple[str, int, int]] = set()
    plan: dict[tuple[str, int], list[int]] = defaultdict(list)
    collect_accounts: set[int] = set()

    for f, d in result.diagnoses:
        if d.action is None:
            continue
        if d.action == COLLECT:
            collect_accounts.add(f.account_id)
            continue
        ok, spent = await can_act(store, d.action, f.account_id, f.chat_pk, now)
        if spent:
            exhausted.add((d.action, f.account_id, f.chat_pk))
        elif ok:
            plan[(d.action, f.account_id)].append(f.chat_pk)

    if act and auto_fix:
        if runtime.is_running("facts", 0):
            result.skipped_reason = "идёт сбор фактов — действия отложены"
        else:
            labels = {f.account_id: f.account_label for f, _d in result.diagnoses}
            done_accounts: set[int] = set()
            # порядок: сначала вступить, потом настроить
            private = {f.chat_pk: f.is_private for f, _d in result.diagnoses}
            limited = {f.account_id for f, _d in result.diagnoses if f.account_spam == "limited"}
            for action in (JOIN, SETUP, RESETUP):
                for (act_name, acc_id), pks in list(plan.items()):
                    if act_name != action:
                        continue
                    if acc_id not in done_accounts and len(done_accounts) >= MAX_ACCOUNTS_PER_CYCLE:
                        continue
                    cap = MAX_PAIRS_PER_ACCOUNT
                    if acc_id in limited:
                        # SpamBlock: сначала пробуем закрытые чаты (там чаще можно писать),
                        # и понемногу — каждая неудача закрывает пару до конца лимита
                        pks = sorted(pks, key=lambda pk: not private.get(pk, False))
                        cap = SPAMBLOCK_PROBES
                    pks = pks[:cap]
                    done_accounts.add(acc_id)
                    for pk in pks:
                        await _record_attempt(store, action, acc_id, pk, now)
                    try:
                        ok, text = await EXECUTORS[action](store, acc_id, pks)
                    except Exception as e:  # noqa: BLE001
                        log.exception("operator action %s failed", action)
                        ok, text = False, f"{type(e).__name__}: {e}"
                    verb = {JOIN: "вступление", SETUP: "настройка", RESETUP: "пересоздание очереди"}[action]
                    result.actions.append(
                        ActionResult(action, acc_id, labels.get(acc_id, str(acc_id)), pks, ok, f"{verb}: {text}")
                    )
            for acc_id in list(collect_accounts)[:3]:
                try:
                    ok, text = await EXECUTORS[COLLECT](store, acc_id, [])
                except Exception as e:  # noqa: BLE001
                    ok, text = False, f"{type(e).__name__}: {e}"
                result.actions.append(
                    ActionResult(COLLECT, acc_id, labels.get(acc_id, str(acc_id)), [], ok, f"пересбор данных: {text}")
                )
            await _remember(store, result.actions, now)

    # Аккаунт с лимитом SpamBot: очередь настроена, но сообщения не уходят даже после
    # пересоздания — значит в этот чат ему нельзя. Закрываем пару до конца лимита (≥ сутки)
    # и пробуем другие чаты, а не долбим этот.
    by_pair = {(f.account_id, f.chat_pk): f for f, _d in result.diagnoses}
    for act_name, acc_id, chat_pk in list(exhausted):
        f = by_pair.get((acc_id, chat_pk))
        if act_name == RESETUP and f is not None and f.account_spam == "limited":
            until = parse_spam_until_safe(f)
            await store.upsert_restriction(
                acc_id,
                chat_pk,
                "spamblock",
                reason="ограничение аккаунта (@SpamBot): сообщения в этот чат не уходят",
                until_at=to_iso(until),
            )
            exhausted.discard((act_name, acc_id, chat_pk))
            result.diagnoses = [
                (pf, Diagnosis(SPAMBLOCKED, "medium", text="SpamBlock: сообщения не уходят"))
                if (pf.account_id, pf.chat_pk) == (acc_id, chat_pk)
                else (pf, dd)
                for pf, dd in result.diagnoses
            ]

    result.escalations = _escalations(result.diagnoses, exhausted)
    result.escalations += _spam_escalations(result.diagnoses)
    result.needs = compute_needs(result.diagnoses, get_settings().priority_keys)
    need = _need_escalation(result.needs)
    if need is not None:
        result.escalations.append(need)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    result.escalations.sort(key=lambda e: (order.get(e.severity, 5), e.title))
    await sync_incidents(store, result)
    await store.set_setting("marketer_last_run_at", to_iso(now))
    return result
