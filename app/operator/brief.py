"""Краткий отчёт оператора: только факты и только то, что нужно человеку."""

from __future__ import annotations

from html import escape

from app.operator.engine import CycleResult, recent_actions
from app.reporting.factual import FactReport, build_fact_report
from app.store import Store
from app.ui.emoji import pe
from app.utils.timefmt import fmt_local


def chat_line(c) -> str:
    gap = "нет отправок" if c.avg_gap is None else f"в среднем раз в {c.avg_gap:.0f} мин"
    mx = f", макс. пауза {c.max_gap} мин" if c.max_gap is not None and c.avg_gap is not None else ""
    plan = f" · в плане {c.working_planned} акк., писали {c.senders_day}"
    bans = f" · бан {c.banned}" if c.banned else ""
    return f"{escape(c.title)}: <b>{c.sends_day}</b> за сутки, {gap}{mx}{plan}{bans}"


async def build_brief(
    store: Store, result: CycleResult, *, morning: bool = False, only_new: list | None = None
) -> str:
    report = await build_fact_report(store)
    tz_day = report.day
    head = "Маркетолог · утренний отчёт" if morning else "Маркетолог"
    lines = [f"{pe('robot')} <b>{head}</b> · <code>{escape(tz_day)}</code>"]

    q = f"{report.accounts_fresh}/{report.accounts_total} акк. с свежими данными"
    if report.last_collect:
        q += f", сбор {fmt_local(report.last_collect, '%H:%M')}"
    lines.append(f"{pe('info')} Данные: {q}")
    if report.data_note:
        lines.append(f"   ⚠ {escape(report.data_note)}")
    lines.append(
        f"{pe('chart')} Отправок за сутки: <b>{report.total_day}</b> (вчера {report.total_prev_day})"
    )
    for c in report.chats:
        if c.priority:
            lines.append(f"{pe('star')} " + chat_line(c))

    dm_first = sum(a.dm_first_24h for a in report.accounts)
    dm_wait = sum(a.dm_waiting for a in report.accounts)
    dm_wrote = sum(a.dm_wrote for a in report.accounts)
    lines.append(
        f"{pe('users')} Люди в ЛС: написали первыми за 24ч <b>{dm_first}</b> · "
        f"всего писали <b>{dm_wrote}</b> · ждут ответа <b>{dm_wait}</b>"
        + (f" · индексация: ещё {report.dm_pending_total} диалогов" if report.dm_pending_total else "")
    )

    actions = await recent_actions(store, 24 if morning else 1)
    if actions:
        lines.append(f"\n{pe('hammer')} <b>Сделал сам</b> ({len(actions)}):")
        for a in actions[-6:]:
            mark = "✓" if a.get("ok") else "✗"
            lines.append(f"{mark} {escape(str(a.get('text'))[:150])}")

    esc = only_new if only_new is not None else result.escalations
    if esc:
        lines.append(f"\n{pe('warn')} <b>Нужно от вас</b> ({len(esc)}):")
        for i, e in enumerate(esc[:6], 1):
            lines.append(f"{i}. {escape(e.title)}\n   → {escape(e.todo)}")
        if len(esc) > 6:
            lines.append(f"… ещё {len(esc) - 6} — в «Проблемы»")
    elif not esc and not only_new:
        lines.append(f"\n{pe('check')} От вас ничего не требуется.")

    return "\n".join(lines)[:3900]
