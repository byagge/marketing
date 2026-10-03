"""Мозг маркетолога: 24/7 сбор → автофикс → суточный снимок → утренний брифинг."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config import get_settings
from app.jobs import LogSink, runtime
from app.marketer.actions import FixResult, apply_fix
from app.marketer.leads import scan_account_leads
from app.marketer.scan import AccountScan, Finding, scan_account
from app.reporting.classify import classify_or_unknown, severity_rank
from app.reporting.density import build_density_report
from app.reporting.stats import local_day
from app.store import Store
from app.ui.emoji import pe

log = logging.getLogger("marketing.marketer")


@dataclass
class MarketerResult:
    accounts_scanned: int = 0
    findings: list[Finding] = field(default_factory=list)
    fixed: list[FixResult] = field(default_factory=list)
    digest: str = ""
    thoughts: list[str] = field(default_factory=list)
    leads_new: int = 0
    leads_messages: int = 0
    density_advice: list[str] = field(default_factory=list)


def _think(
    scans: list[AccountScan],
    findings: list[Finding],
    *,
    density_advice: list[str],
    leads_new: int,
) -> list[str]:
    thoughts: list[str] = []
    crit = [f for f in findings if f.severity == "critical"]
    high = [f for f in findings if f.severity == "high"]

    if leads_new:
        thoughts.append(
            f"За сегодня уже {leads_new} новых лидов в ЛС — канал прогревается."
        )

    if density_advice:
        thoughts.extend(density_advice[:3])

    if not findings and not density_advice:
        thoughts.append(
            "Мониторинг 24/7: schedule и sender в норме. Коплю факты до утреннего брифинга."
        )
        return thoughts

    if crit:
        by_kind: dict[str, int] = {}
        for f in crit:
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        top = ", ".join(
            f"{k}×{v}" for k, v in sorted(by_kind.items(), key=lambda x: -x[1])[:5]
        )
        thoughts.append(f"Критично: {top}. Беру в работу первым.")

    for kind, phrase in (
        ("not_assigned", "пар без факта назначения — чиню schedule"),
        ("schedule_empty", "пустых очередей — переназначаю"),
        ("banned", "банов — нужен ручной вход/другой акк"),
        ("kicked", "киков/нет доступа — вступить заново"),
        ("spam_block", "подозрений на спам-блок — не долблю"),
        ("premium_lost", "Premium слетел — сниму флаг → суточный cron"),
        ("unknown", "неопознанных ошибок — смотри детали в проблемах"),
    ):
        n = sum(1 for f in findings if f.kind == kind)
        if n:
            thoughts.append(f"{n}× {phrase}.")

    if high and not crit:
        thoughts.append(f"{len(high)} проблем high — разгребаю по приоритету.")

    quiet = [s for s in scans if not s.findings]
    if quiet:
        thoughts.append(f"Без замечаний: {len(quiet)} акк. из {len(scans)}.")

    return thoughts[:10]


async def _persist_density(store: Store, day: str) -> list[str]:
    settings = get_settings()
    chats = [c for c in await store.list_chats(kind="schedule") if c.enabled]
    accounts = await store.list_accounts()
    slots = await store.all_slots()
    ok_states = await store.list_setup_states(statuses=["ok"])
    report = build_density_report(
        chats,
        accounts,
        slots,
        ok_states,
        target_min=settings.density_target_min,
        target_max=settings.density_target_max,
    )
    await store.save_density_day(
        day,
        [
            {
                "chat_pk": r.chat_pk,
                "accounts_live": r.accounts_live,
                "avg_gap_min": 0.0 if r.avg_gap_min == float("inf") else r.avg_gap_min,
                "status": r.status,
                "advice": r.advice,
            }
            for r in report.chats
        ],
    )
    # Sparse density → openable incidents
    for row in report.chats:
        if row.status in {"sparse", "empty"}:
            await store.upsert_incident(
                kind="density_sparse",
                severity="high" if row.status == "sparse" else "critical",
                title=f"Редкая подача в «{row.title}»",
                detail=row.advice,
                chat_pk=row.chat_pk,
                source="density",
            )
        elif row.status == "dense":
            await store.upsert_incident(
                kind="density_dense",
                severity="medium",
                title=f"Слишком плотно в «{row.title}»",
                detail=row.advice,
                chat_pk=row.chat_pk,
                source="density",
            )
        else:
            await store.resolve_incidents_matching(
                kind="density_sparse", chat_pk=row.chat_pk
            )
            await store.resolve_incidents_matching(
                kind="density_dense", chat_pk=row.chat_pk
            )
    return report.global_advice


async def _save_day_snapshot(store: Store, day: str, result: MarketerResult) -> None:
    from app.reporting.dashboard import build_dashboard

    dash = await build_dashboard(store, day=day)
    payload = {
        "day": day,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schedule_total": dash.schedule_total,
        "sender_total": dash.sender_total,
        "schedule_avg_per_chat": dash.schedule_avg_per_chat,
        "leads_new": dash.leads_new,
        "leads_messages": dash.leads_messages,
        "open_incidents": len(dash.open_incidents),
        "thoughts": result.thoughts or dash.thoughts,
        "density_advice": result.density_advice,
        "accounts": [
            {
                "id": a.account_id,
                "label": a.label,
                "schedule": a.schedule_msgs,
                "sender": a.sender_msgs,
                "leads": a.leads_new,
                "ok": a.schedule_chats_ok,
                "missing": a.missing_schedule,
            }
            for a in dash.accounts
        ],
        "incidents": [
            {
                "kind": i.kind,
                "severity": i.severity,
                "title": i.title,
                "account": i.account_label,
                "chat": i.chat_title,
                "detail": i.detail[:200],
            }
            for i in dash.open_incidents[:40]
        ],
        "density": await store.density_for_day(day),
        "fixed": [
            {"ok": fx.ok, "message": fx.message, "kind": fx.finding_kind}
            for fx in result.fixed
        ],
    }
    await store.save_daily_report(day, payload)


async def _format_digest(
    store: Store,
    result: MarketerResult,
    label_map: dict[int, str],
    *,
    morning: bool = False,
) -> str:
    settings = get_settings()
    day = local_day(settings.timezone)
    title = "Утренний брифинг" if morning else "Сводка мониторинга"
    lines = [
        f"{pe('robot')} <b>Маркетолог — {title}</b> · <code>{escape(day)}</code>",
        f"Аккаунтов: <b>{result.accounts_scanned}</b> · "
        f"проблем: <b>{len(result.findings)}</b> · "
        f"починил: <b>{sum(1 for x in result.fixed if x.ok)}</b>",
        f"{pe('users')} Лиды сегодня: <b>{result.leads_new}</b> новых · "
        f"<b>{result.leads_messages}</b> сообщ. в ЛС",
        "",
    ]
    if result.thoughts:
        lines.append(f"{pe('cube')} <b>Думаю / план:</b>")
        for t in result.thoughts:
            lines.append(f"· {escape(t)}")
        lines.append("")

    density = await store.density_for_day(day)
    if density:
        lines.append(f"{pe('clock')} <b>Плотность (мин между нашими постами):</b>")
        for d in density[:8]:
            gap = float(d.get("avg_gap_min") or 0)
            st = d.get("status") or ""
            mark = {"ok": "✓", "sparse": "⚠", "dense": "🔥", "empty": "✗"}.get(st, "·")
            title_c = escape(str(d.get("chat_title") or d.get("chat_pk")))
            lines.append(
                f"{mark} {title_c}: <b>{gap:.1f} мин</b> "
                f"({d.get('accounts_live', 0)} акк.)"
            )
        lines.append("")

    ordered = sorted(result.findings, key=lambda f: (severity_rank(f.severity), f.kind))
    if ordered:
        lines.append(f"{pe('warn')} <b>Все проблемы (приоритет):</b>")
        for f in ordered[:20]:
            sev = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(
                f.severity, "⚪"
            )
            who = escape(label_map.get(f.account_id or 0, f"#{f.account_id or '—'}"))
            chat_bit = ""
            if f.chat_pk:
                chat = await store.get_chat(f.chat_pk)
                if chat:
                    chat_bit = f" / {escape(chat.display_name)}"
            lines.append(
                f"{sev} <b>{escape(f.title)}</b> — {who}{chat_bit}\n"
                f"   <i>{escape((f.detail or '')[:140])}</i>"
            )
        if len(ordered) > 20:
            lines.append(f"… ещё {len(ordered) - 20}")
        lines.append("")

    if result.fixed:
        lines.append(f"{pe('hammer')} <b>Сделал сам:</b>")
        for fx in result.fixed[:12]:
            mark = pe("check") if fx.ok else pe("block")
            lines.append(f"{mark} {escape(fx.message)}")

    lines.append("")
    lines.append(
        f"{pe('info')} Факты копятся 24/7. Полный отчёт за любой день — в Отчётности."
    )
    return "\n".join(lines)[:3900]


async def run_marketer_once(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    notify: bool = False,
    morning: bool = False,
    account_ids: list[int] | None = None,
) -> MarketerResult:
    """Один цикл: сбор фактов + лиды + плотность + автофикс + снимок дня.

    notify=False — тихий 24/7 сбор (критичное можно всё же пушнуть).
    morning=True — полный брифинг админу (07:40).
    """
    enabled = (await store.get_setting("marketer_enabled", "1")) == "1"
    if not enabled and account_ids is None and not morning:
        return MarketerResult(digest="Маркетолог выключен")

    settings = get_settings()
    auto_fix = (await store.get_setting("marketer_auto_fix", "1")) == "1"
    result = MarketerResult()
    day = local_day(settings.timezone)

    job = await store.create_job("marketer_morning" if morning else "marketer", None)
    sink = LogSink(store, job.id, None, None)
    await sink.emit(
        "Маркетолог: утренний брифинг…" if morning else "Маркетолог: тихий сбор…",
        notify=False,
    )

    accounts = await store.list_accounts()
    if account_ids is not None:
        want = set(account_ids)
        accounts = [a for a in accounts if a.id in want]

    scans: list[AccountScan] = []
    for acc in accounts:
        if runtime.cancelled("marketer", 0):
            await sink.emit("Маркетолог: остановка", "warn")
            break
        try:
            scan = await scan_account(store, acc)
            leads = await scan_account_leads(store, acc)
            result.leads_new += leads.new_leads
            result.leads_messages += leads.messages
            if leads.error:
                scan.findings.append(
                    Finding(
                        kind="leads_scan",
                        severity="medium",
                        title="Не смог собрать лиды (ЛС)",
                        detail=leads.error,
                        account_id=acc.id,
                    )
                )
            scans.append(scan)
            result.accounts_scanned += 1
            result.findings.extend(scan.findings)
            for f in scan.findings:
                # Абсолютно все findings → incidents (включая unknown)
                await store.upsert_incident(
                    kind=f.kind,
                    severity=f.severity,
                    title=f.title,
                    detail=f.detail,
                    account_id=f.account_id,
                    chat_pk=f.chat_pk,
                    source="marketer",
                )
        except Exception as e:
            log.exception("scan failed acc=%s", acc.id)
            spec = classify_or_unknown(f"{type(e).__name__}: {e}")
            finding = Finding(
                kind=spec.kind,
                severity=spec.severity,
                title=spec.title_ru,
                detail=f"Скан аккаунта: {e}",
                account_id=acc.id,
            )
            result.findings.append(finding)
            await store.upsert_incident(
                kind=finding.kind,
                severity=finding.severity,
                title=finding.title,
                detail=finding.detail,
                account_id=acc.id,
                source="marketer",
            )

    result.density_advice = await _persist_density(store, day)

    fixable = [
        f
        for f in sorted(result.findings, key=lambda x: severity_rank(x.severity))
        if f.auto_fixable
    ]
    if auto_fix and fixable:
        for finding in fixable[:12]:
            if runtime.cancelled("marketer", 0):
                break
            fx = await apply_fix(
                store, finding, bot=bot, admin_chat_id=admin_chat_id
            )
            result.fixed.append(fx)
            await sink.emit(fx.message, "info" if fx.ok else "error", notify=False)

    result.thoughts = _think(
        scans,
        result.findings,
        density_advice=result.density_advice,
        leads_new=result.leads_new,
    )
    label_map = {s.account_id: s.label for s in scans}
    result.digest = await _format_digest(
        store, result, label_map, morning=morning
    )

    await _save_day_snapshot(store, day, result)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await store.set_setting("marketer_last_run_at", now)
    await store.finish_job(job.id, "done", result.digest[:1500])

    # Утро — всегда шлём. Иначе — только critical, если notify/критичное всплыло.
    crit = [f for f in result.findings if f.severity == "critical"]
    should_ping = morning or (notify and crit)
    if should_ping and bot and admin_chat_id:
        try:
            await bot.send_message(admin_chat_id, result.digest, parse_mode="HTML")
            await store.set_setting("marketer_last_digest_at", now)
        except Exception:
            log.exception("digest send failed")

    return result


def yesterday_local(tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    return (datetime.now(tz).date() - timedelta(days=1)).isoformat()
