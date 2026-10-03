"""Сводка для экрана «Отчётность» — брифинг + история по дням."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.models import Incident, MinuteSlot, SetupState
from app.reporting.stats import expected_per_chat_24h, local_day
from app.store import Store
from app.ui.emoji import pe


@dataclass
class AccountSendRow:
    account_id: int
    label: str
    schedule_msgs: float = 0.0
    sender_msgs: float = 0.0
    schedule_chats_ok: int = 0
    schedule_chats_total: int = 0
    missing_schedule: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    leads_new: int = 0
    leads_messages: int = 0


@dataclass
class Dashboard:
    day: str
    schedule_total: float
    sender_total: float
    schedule_avg_per_chat: float
    schedule_chat_count: int
    open_incidents: list[Incident]
    accounts: list[AccountSendRow]
    marketer_last_run: str = ""
    marketer_enabled: bool = True
    auto_fix: bool = True
    thoughts: list[str] = field(default_factory=list)
    leads_new: int = 0
    leads_messages: int = 0
    density_rows: list[dict] = field(default_factory=list)
    density_advice: list[str] = field(default_factory=list)
    from_archive: bool = False


async def build_dashboard(store: Store, *, day: str | None = None) -> Dashboard:
    settings = get_settings()
    day = day or local_day(settings.timezone)

    # Архив: если есть сохранённый payload и день не сегодня — можно подмешать
    archived = await store.get_daily_report(day)
    today = local_day(settings.timezone)

    chats = [c for c in await store.list_chats(kind="schedule") if c.enabled]
    accounts = await store.list_accounts()
    slots = await store.all_slots()
    active = await store.active_slots()
    ok_states = await store.list_setup_states(statuses=["ok"])
    pending = await store.list_setup_states(statuses=["pending", "abandoned"])
    incidents = await store.list_incidents(statuses=["open", "fixing"], limit=50)
    stats = await store.send_stats_for_day(day)
    lead_rows = await store.lead_stats_for_day(day)
    density_rows = await store.density_for_day(day)

    chat_by_id = {c.id: c for c in chats}
    active_keys = {(s.account_id, s.chat_pk) for s in active}
    slot_by_acc: dict[int, list[MinuteSlot]] = defaultdict(list)
    for s in slots:
        if s.chat_pk in chat_by_id:
            slot_by_acc[s.account_id].append(s)

    ok_by_acc: dict[int, list[SetupState]] = defaultdict(list)
    for st in ok_states:
        ok_by_acc[st.account_id].append(st)

    schedule_msgs: dict[int, float] = defaultdict(float)
    sender_msgs: dict[int, float] = defaultdict(float)
    for row in stats:
        if row.channel == "schedule":
            schedule_msgs[row.account_id] += row.messages
        elif row.channel == "sender":
            sender_msgs[row.account_id] += row.messages

    leads_by_acc = {r.account_id: r for r in lead_rows}
    leads_new = sum(r.new_leads for r in lead_rows)
    leads_messages = sum(r.messages for r in lead_rows)

    account_rows: list[AccountSendRow] = []
    thoughts: list[str] = []

    for acc in accounts:
        sch = schedule_msgs.get(acc.id, 0.0)
        snd = sender_msgs.get(acc.id, 0.0)
        acc_slots = slot_by_acc.get(acc.id, [])
        ok_n = len(ok_by_acc.get(acc.id, []))
        missing: list[str] = []
        for slot in acc_slots:
            if (acc.id, slot.chat_pk) in active_keys:
                continue
            chat = chat_by_id.get(slot.chat_pk)
            title = chat.display_name if chat else slot.chat_title or f"#{slot.chat_pk}"
            missing.append(title)

        problems: list[str] = []
        for inc in incidents:
            if inc.account_id == acc.id:
                problems.append(
                    f"{inc.title}" + (f" «{inc.chat_title}»" if inc.chat_title else "")
                )

        if missing and day == today:
            thoughts.append(
                f"⚠ {acc.label}: в плане есть, факт нет — "
                + ", ".join(missing[:4])
                + ("…" if len(missing) > 4 else "")
            )

        lr = leads_by_acc.get(acc.id)
        account_rows.append(
            AccountSendRow(
                account_id=acc.id,
                label=acc.label,
                schedule_msgs=sch,
                sender_msgs=snd,
                schedule_chats_ok=ok_n,
                schedule_chats_total=len(acc_slots) or ok_n,
                missing_schedule=missing,
                problems=problems[:8],
                leads_new=lr.new_leads if lr else 0,
                leads_messages=lr.messages if lr else 0,
            )
        )

    open_crit = [i for i in incidents if i.severity == "critical"]
    if day == today:
        if open_crit:
            thoughts.insert(
                0,
                f"Сейчас {len(open_crit)} критических — в приоритете.",
            )
        elif not incidents:
            thoughts.insert(0, "Критических сбоев нет. Мониторинг 24/7 идёт.")

    pending_n = len(pending)
    if pending_n and day == today:
        thoughts.append(f"В retry-очереди {pending_n} пар аккаунт×чат.")

    density_advice: list[str] = []
    sparse = [d for d in density_rows if d.get("status") in {"sparse", "empty"}]
    if sparse:
        density_advice.append(
            f"{len(sparse)} чатов с редкой подачей (>10 мин) — нужны аккаунты."
        )
    for d in density_rows:
        if d.get("status") == "sparse" and d.get("advice"):
            density_advice.append(str(d["advice"])[:180])
            break

    sch_total = sum(schedule_msgs.values())
    snd_total = sum(sender_msgs.values())
    n_chats = max(1, len(chats))
    avg = (sch_total / n_chats) if sch_total > 0 else 0.0

    marketer_on = (await store.get_setting("marketer_enabled", "1")) == "1"
    auto_fix = (await store.get_setting("marketer_auto_fix", "1")) == "1"
    last_run = await store.get_setting("marketer_last_run_at", "")

    # Если смотрим архивный день и live-цифры пустые — подтянуть payload
    from_archive = False
    if archived and day != today and sch_total == 0 and leads_new == 0:
        try:
            payload = json.loads(archived.payload_json or "{}")
            sch_total = float(payload.get("schedule_total") or 0)
            snd_total = float(payload.get("sender_total") or 0)
            avg = float(payload.get("schedule_avg_per_chat") or 0)
            leads_new = int(payload.get("leads_new") or 0)
            leads_messages = int(payload.get("leads_messages") or 0)
            thoughts = list(payload.get("thoughts") or thoughts)
            density_advice = list(payload.get("density_advice") or density_advice)
            density_rows = list(payload.get("density") or density_rows)
            from_archive = True
        except Exception:
            pass

    return Dashboard(
        day=day,
        schedule_total=sch_total,
        sender_total=snd_total,
        schedule_avg_per_chat=avg,
        schedule_chat_count=len(chats),
        open_incidents=incidents if day == today else [],
        accounts=account_rows,
        marketer_last_run=last_run,
        marketer_enabled=marketer_on,
        auto_fix=auto_fix,
        thoughts=(thoughts + density_advice)[:12],
        leads_new=leads_new,
        leads_messages=leads_messages,
        density_rows=density_rows,
        density_advice=density_advice,
        from_archive=from_archive,
    )


def format_dashboard_html(dash: Dashboard) -> str:
    arch = " · архив" if dash.from_archive else ""
    lines = [
        f"{pe('chart')} <b>Отчётность</b> · <code>{escape(dash.day)}</code>{arch}\n",
        f"{pe('clock')} Schedule: <b>{dash.schedule_total:.0f}</b> "
        f"(ср./чат <b>{dash.schedule_avg_per_chat:.1f}</b> · "
        f"чатов {dash.schedule_chat_count})",
        f"{pe('mega')} Sender: <b>{dash.sender_total:.0f}</b>",
        f"{pe('users')} Лиды: <b>{dash.leads_new}</b> новых · "
        f"<b>{dash.leads_messages}</b> в ЛС",
        "",
    ]

    if dash.density_rows:
        lines.append(f"{pe('pin')} <b>Плотность (мин между постами):</b>")
        for d in dash.density_rows[:5]:
            gap = float(d.get("avg_gap_min") or 0)
            st = d.get("status") or ""
            mark = {"ok": "✓", "sparse": "⚠", "dense": "🔥", "empty": "✗"}.get(st, "·")
            title = escape(str(d.get("chat_title") or d.get("chat_pk")))[:40]
            lines.append(
                f"{mark} {title}: <b>{gap:.1f} мин</b> / {int(d.get('accounts_live') or 0)} акк."
            )
        lines.append("")

    if dash.thoughts:
        lines.append(f"{pe('robot')} <b>Маркетолог:</b>")
        for t in dash.thoughts[:5]:
            lines.append(f"· {escape(t)[:180]}")
        lines.append("")

    open_n = len(dash.open_incidents)
    if open_n:
        lines.append(f"{pe('warn')} <b>Проблемы:</b> {open_n}")
        for inc in dash.open_incidents[:6]:
            who = escape(inc.account_label or "—")[:24]
            chat = f" / {escape(inc.chat_title)[:28]}" if inc.chat_title else ""
            sev = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(
                inc.severity, "⚪"
            )
            lines.append(f"{sev} <b>{escape(inc.title)[:80]}</b> — {who}{chat}")
        if open_n > 6:
            lines.append(f"… ещё {open_n - 6}")
        lines.append("")

    lines.append(f"{pe('user')} <b>По аккаунтам</b>")
    for row in dash.accounts[:12]:
        miss = (
            f" ⚠нет факта:{len(row.missing_schedule)}"
            if row.missing_schedule
            else ""
        )
        leads = (
            f" · лиды <code>{row.leads_new}</code>"
            if row.leads_new or row.leads_messages
            else ""
        )
        lines.append(
            f"· <b>{escape(row.label)[:20]}</b> — "
            f"sch <code>{row.schedule_msgs:.0f}</code> "
            f"({row.schedule_chats_ok}/{row.schedule_chats_total}) · "
            f"snd <code>{row.sender_msgs:.0f}</code>{leads}{miss}"
        )
    if len(dash.accounts) > 12:
        lines.append(f"… ещё {len(dash.accounts) - 12} акк.")

    status = "24/7 вкл" if dash.marketer_enabled else "выкл"
    last = escape(dash.marketer_last_run or "—")[:25]
    lines.append("")
    lines.append(
        f"{pe('robot')} Мониторинг: <b>{status}</b> · утро 7:40 · "
        f"<code>{last}</code>"
    )
    return _safe_html_trim("\n".join(lines), 3500)


def _safe_html_trim(text: str, limit: int = 3500) -> str:
    """Обрезка HTML без разрыва тегов (иначе Telegram BadRequest)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # незакрытый открывающий тег в конце
    last_lt = cut.rfind("<")
    last_gt = cut.rfind(">")
    if last_lt > last_gt:
        cut = cut[:last_lt]
    # незакрытый </... в конце
    close = cut.rfind("</")
    if close != -1 and cut.find(">", close) == -1:
        cut = cut[:close]
    return cut.rstrip() + "\n…"


def day_offsets(tz_name: str, n: int = 7) -> list[tuple[int, str]]:
    """[(offset, ISO day)] 0=today …"""
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    return [(i, (today - timedelta(days=i)).isoformat()) for i in range(n)]
