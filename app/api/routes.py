"""Все отчёты и данные каталога по HTTP."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.auth import require_api_key
from app.api.serialize import to_jsonable
from app.store import Store

router = APIRouter(dependencies=[Depends(require_api_key)])


def _store(request: Request) -> Store:
    return request.app.state.store


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "marketing", "ts": datetime.now(timezone.utc).isoformat()}


# ---- каталог -----------------------------------------------------------------

@router.get("/accounts")
async def list_accounts(request: Request) -> dict[str, Any]:
    store = _store(request)
    rows = [to_jsonable(a) for a in await store.list_accounts()]
    return {"count": len(rows), "items": rows}


@router.get("/accounts/{account_id}")
async def get_account(account_id: int, request: Request) -> dict[str, Any]:
    acc = await _store(request).get_account(account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    return to_jsonable(acc)


@router.get("/chats")
async def list_chats(
    request: Request,
    kind: str | None = None,
    enabled_only: bool = False,
) -> dict[str, Any]:
    rows = [
        to_jsonable(c)
        for c in await _store(request).list_chats(kind=kind, enabled_only=enabled_only)
    ]
    return {"count": len(rows), "items": rows}


@router.get("/chats/{chat_pk}")
async def get_chat(chat_pk: int, request: Request) -> dict[str, Any]:
    chat = await _store(request).get_chat(chat_pk)
    if not chat:
        raise HTTPException(404, "chat not found")
    return to_jsonable(chat)


@router.get("/schedule")
async def schedule_all(request: Request) -> dict[str, Any]:
    slots = await _store(request).active_slots()
    return {"count": len(slots), "items": [to_jsonable(s) for s in slots]}


@router.get("/schedule/{chat_pk}")
async def schedule_slots(chat_pk: int, request: Request) -> dict[str, Any]:
    store = _store(request)
    chat = await store.get_chat(chat_pk)
    if not chat:
        raise HTTPException(404, "chat not found")
    slots = await store.active_slots_for_chat(chat_pk)
    return {"chat": to_jsonable(chat), "slots": [to_jsonable(s) for s in slots]}


@router.get("/settings/sender")
async def sender_settings(request: Request) -> dict[str, Any]:
    return to_jsonable(await _store(request).sender_settings())


# ---- отчёты ------------------------------------------------------------------

@router.get("/reports/dashboard")
async def report_dashboard(request: Request, day: str | None = None) -> dict[str, Any]:
    from app.reporting.dashboard import build_dashboard, format_dashboard_html

    dash = await build_dashboard(_store(request), day=day)
    return {
        "data": to_jsonable(dash),
        "html": format_dashboard_html(dash),
    }


@router.get("/reports/facts")
async def report_facts(request: Request, day: str | None = None) -> dict[str, Any]:
    from app.reporting.factual import build_fact_report

    return {"data": to_jsonable(await build_fact_report(_store(request), day))}


@router.get("/reports/density")
async def report_density(request: Request, day: str | None = None) -> dict[str, Any]:
    from app.reporting.dashboard import format_density_html
    from app.reporting.factual import build_fact_report

    rep = await build_fact_report(_store(request), day)
    return {"data": to_jsonable(rep), "html": format_density_html(rep)}


@router.get("/reports/accounts")
async def report_accounts(
    request: Request,
    day: str | None = None,
    page: int = Query(0, ge=0),
) -> dict[str, Any]:
    from app.reporting.dashboard import format_accounts_html
    from app.reporting.factual import build_fact_report

    rep = await build_fact_report(_store(request), day)
    html, pages = format_accounts_html(rep, page)
    return {
        "data": to_jsonable(rep.accounts),
        "html": html,
        "page": page,
        "pages": pages,
    }


@router.get("/reports/leads")
async def report_leads(request: Request, day: str | None = None) -> dict[str, Any]:
    from app.reporting.factual import build_fact_report

    rep = await build_fact_report(_store(request), day)
    return {
        "day": rep.day,
        "dm_people_day": getattr(rep, "dm_people_day", None),
        "dm_first_day": getattr(rep, "dm_first_day", None),
        "accounts": [
            {
                "id": a.account_id if hasattr(a, "account_id") else getattr(a, "id", None),
                "label": a.label,
                **{
                    k: getattr(a, k, None)
                    for k in (
                        "dm_wrote",
                        "dm_first",
                        "dm_waiting",
                        "leads_day",
                        "leads_first",
                    )
                    if hasattr(a, k)
                },
            }
            for a in rep.accounts
        ],
        "data": to_jsonable(rep),
    }


@router.get("/reports/coverage")
async def report_coverage(request: Request) -> dict[str, Any]:
    from app.jobs.coverage import build_coverage, coverage_report

    store = _store(request)
    cov = await build_coverage(store)
    return {"data": to_jsonable(cov), "text": await coverage_report(store)}


@router.get("/reports/advisor")
async def report_advisor(request: Request) -> dict[str, Any]:
    from app.jobs.advisor import build_advice

    advice = await build_advice(_store(request))
    return {"data": to_jsonable(advice)}


@router.get("/reports/marketer")
async def report_marketer(
    request: Request,
    act: bool = Query(False, description="выполнить действия оператора"),
) -> dict[str, Any]:
    from app.operator.brief import build_brief
    from app.operator.engine import run_cycle

    store = _store(request)
    cycle = await run_cycle(store, None, None, act=act)
    brief = await build_brief(store, cycle, morning=True)
    return {
        "pairs": cycle.pairs,
        "working": cycle.working,
        "by_cause": dict(cycle.by_cause),
        "actions": to_jsonable(cycle.actions),
        "escalations": to_jsonable(cycle.escalations),
        "brief_html": brief,
    }


@router.get("/reports/redesign")
async def report_redesign(request: Request) -> dict[str, Any]:
    from app.jobs.perf import redesign_candidates, redesign_report_html

    items = await redesign_candidates(_store(request))
    return {
        "items": [
            {"account": to_jsonable(acc), "perf": to_jsonable(perf)}
            for acc, perf in items
        ],
        "html": await redesign_report_html(_store(request)),
    }


@router.get("/reports/export")
async def report_export(request: Request) -> dict[str, Any]:
    from app.jobs.export import build_report, render_summary

    report = await build_report(_store(request))
    return {"data": to_jsonable(report), "summary": render_summary(report)}


@router.get("/incidents")
async def incidents(
    request: Request,
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    statuses = [status] if status else ["open", "fixing", "done"]
    rows = await _store(request).list_incidents(statuses=statuses, limit=limit)
    return {"count": len(rows), "items": [to_jsonable(r) for r in rows]}


@router.get("/jobs")
async def jobs(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    store = _store(request)
    if hasattr(store, "recent_jobs"):
        rows = await store.recent_jobs(limit=limit)
    else:
        rows = []
    return {"count": len(rows), "items": [to_jsonable(r) for r in rows]}


@router.get("/restrictions")
async def restrictions(request: Request) -> dict[str, Any]:
    store = _store(request)
    # chat_restrictions / list helpers vary — отдаём то, что есть
    out: dict[str, Any] = {}
    if hasattr(store, "list_active_restrictions"):
        out["active"] = to_jsonable(await store.list_active_restrictions())
    if hasattr(store, "list_restr_events"):
        out["events"] = to_jsonable(await store.list_restr_events(limit=200))
    elif hasattr(store, "recent_restr_events"):
        out["events"] = to_jsonable(await store.recent_restr_events(limit=200))
    return out


@router.get("/facts/window")
async def facts_window(
    request: Request,
    minutes: int = Query(60, ge=5, le=24 * 60),
) -> dict[str, Any]:
    from datetime import timedelta

    from app.config import get_settings
    from app.ui.facts_view import window_view

    tz = get_settings().tz
    end = datetime.now(tz).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=minutes)
    text, png = await window_view(
        _store(request), start, minutes, title=f"Факты · {minutes} мин"
    )
    return {"text": text, "has_png": bool(png), "start": start.isoformat()}


@router.get("/facts/day")
async def facts_day(
    request: Request,
    days_ago: int = Query(0, ge=0, le=30),
) -> dict[str, Any]:
    from app.ui.facts_view import day_view

    text, png, hours = await day_view(_store(request), days_ago)
    return {"text": text, "has_png": bool(png), "hours": hours, "days_ago": days_ago}


@router.get("/send-events")
async def send_events(
    request: Request,
    since: str = Query(..., description="ISO UTC, например 2026-10-10T00:00:00+00:00"),
    account_id: int | None = None,
    chat_pk: int | None = None,
    limit: int = Query(5000, ge=1, le=50000),
) -> dict[str, Any]:
    store = _store(request)
    # прямой SQL через существующие методы, если есть
    if hasattr(store, "list_send_events"):
        rows = await store.list_send_events(
            since=since, account_id=account_id, chat_pk=chat_pk, limit=limit
        )
        return {"count": len(rows), "items": to_jsonable(rows)}
    import aiosqlite

    sql = "SELECT id, account_id, chat_pk, sent_at, msg_id FROM send_events WHERE sent_at>=?"
    args: list[Any] = [since]
    if account_id is not None:
        sql += " AND account_id=?"
        args.append(account_id)
    if chat_pk is not None:
        sql += " AND chat_pk=?"
        args.append(chat_pk)
    sql += " ORDER BY sent_at DESC LIMIT ?"
    args.append(limit)
    async with aiosqlite.connect(store.path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        rows = [dict(r) for r in await cur.fetchall()]
    return {"count": len(rows), "items": rows}


@router.get("/chat-facts")
async def chat_facts(request: Request) -> dict[str, Any]:
    store = _store(request)
    if hasattr(store, "list_facts"):
        return {"items": to_jsonable(await store.list_facts())}
    import aiosqlite

    async with aiosqlite.connect(store.path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chat_facts")
        rows = [dict(r) for r in await cur.fetchall()]
    return {"count": len(rows), "items": rows}


@router.get("/dm/people")
async def dm_people(
    request: Request,
    account_id: int | None = None,
    started_by: str | None = None,
    limit: int = Query(1000, ge=1, le=20000),
) -> dict[str, Any]:
    import aiosqlite

    store = _store(request)
    sql = "SELECT * FROM dm_people WHERE 1=1"
    args: list[Any] = []
    if account_id is not None:
        sql += " AND account_id=?"
        args.append(account_id)
    if started_by:
        sql += " AND started_by=?"
        args.append(started_by)
    sql += " ORDER BY last_in_at DESC LIMIT ?"
    args.append(limit)
    async with aiosqlite.connect(store.path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        rows = [dict(r) for r in await cur.fetchall()]
    return {"count": len(rows), "items": rows}


@router.get("/premium")
async def premium_status(request: Request) -> dict[str, Any]:
    from app.tg.premium import hours_left, needs_warn

    items = []
    for a in await _store(request).list_accounts():
        items.append(
            {
                "id": a.id,
                "label": a.label,
                "is_premium": bool(a.is_premium),
                "premium_since": a.premium_since,
                "premium_until": a.premium_until,
                "premium_until_source": a.premium_until_source,
                "hours_left": hours_left(a.premium_until) if a.premium_until else None,
                "warn": needs_warn(a.premium_until) if a.premium_until else False,
            }
        )
    return {"count": len(items), "items": items}


@router.get("/setup-states")
async def setup_states(
    request: Request,
    account_id: int | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    statuses = [status] if status else None
    rows = await _store(request).list_setup_states(account_id, statuses=statuses)
    return {"count": len(rows), "items": [to_jsonable(r) for r in rows]}
