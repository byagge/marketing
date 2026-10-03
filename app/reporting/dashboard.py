"""Экран «Отчётность»: реальные данные по истории чатов и диалогов (без оценок)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from app.models import Incident
from app.operator.brief import chat_line
from app.reporting.factual import AccountFact, FactReport, build_fact_report
from app.store import Store
from app.ui.emoji import pe
from app.utils.coverage import accounts_needed
from app.utils.timefmt import fmt_local


@dataclass
class Dashboard:
    day: str
    report: FactReport
    open_incidents: list[Incident] = field(default_factory=list)
    marketer_enabled: bool = True
    auto_fix: bool = True
    marketer_last_run: str = ""


async def build_dashboard(store: Store, *, day: str | None = None) -> Dashboard:
    report = await build_fact_report(store, day)
    incidents = await store.list_incidents(statuses=["open", "fixing"], limit=60)
    return Dashboard(
        day=report.day,
        report=report,
        open_incidents=incidents if report.is_today else [],
        marketer_enabled=(await store.get_setting("marketer_enabled", "1")) == "1",
        auto_fix=(await store.get_setting("marketer_auto_fix", "1")) == "1",
        marketer_last_run=await store.get_setting("marketer_last_run_at", ""),
    )


def spam_summary(accounts: list[AccountFact]) -> str:
    limited = [a.label for a in accounts if a.spam_status == "limited"]
    unchecked = sum(1 for a in accounts if not a.spam_status)
    clean = sum(1 for a in accounts if a.spam_status == "clean")
    text = f"чистых {clean}, ограничено {len(limited)}"
    if limited:
        text += f" ({escape(', '.join(limited[:6]))})"
    if unchecked:
        text += f", не проверено {unchecked}"
    return text


def format_dashboard_html(dash: Dashboard) -> str:
    r = dash.report
    tag = " · сегодня" if r.is_today else ""
    lines = [f"{pe('chart')} <b>Отчётность</b> · <code>{escape(dash.day)}</code>{tag}"]
    q = f"{r.accounts_fresh}/{r.accounts_total} акк. с свежими данными"
    if r.last_collect:
        q += f" · сбор {fmt_local(r.last_collect, '%d.%m %H:%M')}"
    lines.append(f"{pe('info')} Данные: {q}")
    if r.data_note:
        lines.append(f"   ⚠ {escape(r.data_note)}")
    lines.append("")
    lines.append(
        f"{pe('mega')} Отправок за сутки: <b>{r.total_day}</b> "
        f"(вчера {r.total_prev_day}) · чатов {len(r.chats)}"
    )
    for c in r.chats:
        if c.priority:
            lines.append(f"{pe('star')} " + chat_line(c))

    weak = [c for c in r.chats if not c.priority and (c.avg_gap is None or (c.max_gap or 0) >= 45)]
    if weak:
        lines.append(f"\n{pe('warn')} <b>Слабые чаты</b> (нет отправок или пауза ≥45 мин):")
        for c in weak[:5]:
            lines.append("· " + chat_line(c))
        if len(weak) > 5:
            lines.append(f"… ещё {len(weak) - 5}")

    new_day = sum(a.dm_first_day for a in r.accounts)
    active_day = sum(a.dm_active_day for a in r.accounts)
    waiting = sum(a.dm_waiting for a in r.accounts)
    lines.append(
        f"\n{pe('users')} <b>Люди в ЛС за сутки:</b> новых <b>{new_day}</b> · "
        f"писали <b>{active_day}</b>"
        + (f" · ждут ответа <b>{waiting}</b>" if r.is_today else "")
    )
    top = [a for a in r.accounts if a.dm_first_day or a.dm_active_day][:5]
    for a in top:
        lines.append(
            f"· <b>{escape(a.label)[:18]}</b>: новых {a.dm_first_day}, писали {a.dm_active_day}, "
            f"всего писали {a.dm_wrote}"
        )
    if r.dm_pending_total:
        lines.append(f"   <i>индексация ЛС: ещё {r.dm_pending_total} диалогов — цифры растут</i>")

    lines.append(f"\n{pe('shield')} SpamBot: {spam_summary(r.accounts)}")

    inc = dash.open_incidents
    if inc:
        lines.append(f"\n{pe('warn')} <b>Проблемы</b> ({len(inc)}):")
        for i in inc[:6]:
            sev = {"critical": "🔴", "high": "🟠", "medium": "🟡"}.get(i.severity, "⚪")
            lines.append(f"{sev} {escape(i.title)[:140]}")
        if len(inc) > 6:
            lines.append(f"… ещё {len(inc) - 6}")
    elif r.is_today:
        lines.append(f"\n{pe('check')} Открытых проблем нет.")

    status = "вкл" if dash.marketer_enabled else "выкл"
    last = fmt_local(dash.marketer_last_run) if dash.marketer_last_run else "—"
    lines.append(f"\n{pe('robot')} Оператор: <b>{status}</b> · последний цикл {last}")
    return _safe_html_trim("\n".join(lines), 3800)


def format_density_html(report: FactReport) -> str:
    lines = [f"Реальная частота за <code>{escape(report.day)}</code> "
             f"(по сообщениям в истории чатов)\n"]
    if report.data_note:
        lines.append(f"⚠ {escape(report.data_note)}\n")
    for c in report.chats:
        need = accounts_needed(c.interval)  # цель: пауза ≤5 мин
        deficit = max(0, need - c.senders_day)
        star = "⭐ " if c.priority else ""
        gap = "—" if c.avg_gap is None else f"{c.avg_gap:.0f}"
        med = "" if c.median_gap is None else f", медиана {c.median_gap:.0f}"
        mx = "" if c.max_gap is None else f", макс. {c.max_gap}"
        lines.append(
            f"{star}<b>{escape(c.title)}</b>\n"
            f"   {c.sends_day} отпр. · в среднем раз в {gap} мин{med}{mx}\n"
            f"   писали {c.senders_day} акк. (в плане {c.working_planned})"
            + (f" · бан {c.banned}" if c.banned else "")
            + (f" · мут {c.muted}" if c.muted else "")
            + (f"\n   до цели 5 мин не хватает ≈{deficit} акк." if deficit else "")
        )
    return "\n".join(lines)


def format_accounts_html(report: FactReport, page: int, per: int = 8) -> tuple[str, int]:
    total = max(1, (len(report.accounts) + per - 1) // per)
    page = max(0, min(page, total - 1))
    chunk = report.accounts[page * per : page * per + per]
    lines = [f"{pe('user')} <b>По аккаунтам</b> · <code>{escape(report.day)}</code> · стр. {page + 1}/{total}\n"]
    for a in chunk:
        spam = {"clean": "✓", "limited": "⛔ ограничен", "unknown": "?"}.get(a.spam_status, "не проверен")
        if a.spam_status == "limited" and a.spam_until:
            spam += f" до {escape(a.spam_until)}"
        data = "" if a.facts_status != "err" else f" · ⚠ данные не читаются ({escape(a.facts_err[:40])})"
        lines.append(
            f"<b>{escape(a.label)}</b>: отправок {a.sends_day} в {a.chats_day} чатах "
            f"(в плане {a.planned_chats}) · SpamBot {spam}{data}\n"
            f"   ЛС: новых за день {a.dm_first_day}, писали {a.dm_active_day} · "
            f"всего писали {a.dm_wrote} (первыми {a.dm_first_total}) · ждут ответа {a.dm_waiting}"
            + (f" · бан {a.banned}" if a.banned else "")
            + (f" · мут {a.muted}" if a.muted else "")
        )
    return "\n".join(lines), total


def _safe_html_trim(text: str, limit: int = 3800) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_lt = cut.rfind("<")
    if last_lt > cut.rfind(">"):
        cut = cut[:last_lt]
    return cut.rstrip() + "\n…"


def day_offsets(tz_name: str, n: int = 7) -> list[tuple[int, str]]:
    """[(offset, ISO day)] 0=today …"""
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    return [(i, (today - timedelta(days=i)).isoformat()) for i in range(n)]
