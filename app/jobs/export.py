"""Полная выгрузка состояния для разбора: ZIP с JSON + CSV + SUMMARY.md.

Цель — отдать одним файлом ВСЁ, что знает бот (аккаунты, чаты, слоты, состояния
настройки, факты, задачи, логи, настройки), плюс уже посчитанные аномалии, чтобы
разбор ошибок не требовал доступа к серверу. Секреты (сессии, токены) не
выгружаются; телефоны и invite-ссылки маскируются.
"""

from __future__ import annotations

import csv
import io
import json
import platform
import subprocess
import zipfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot

from app.config import ROOT, get_settings
from app.jobs import maintenance_lock, runtime
from app.jobs.balance import ChatAnalysis, analyze_state
from app.sender_api import SenderAPI
from app.store import Store
from app.utils.balance import (
    MUTED,
    NOT_MEMBER,
    SILENT,
    UNKNOWN,
    WORKING,
    accounts_needed,
    circle_length,
    positions,
)
from app.utils.schedule import posts_count_for_interval

MAX_JOBS = 3000
MAX_LOGS = 25000

NOT_AVAILABLE = [
    "Входящие ЛС, «ждут ответа», кто написал первым — это считает маркетолог на сервере, "
    "в базе бота этих данных нет. Приложите его текстовые отчёты рядом с этим файлом.",
    "Состояние @SpamBot (ограничения аккаунтов) — тоже у маркетолога; здесь только то, "
    "что видно по правам в чатах (мут/запрет).",
    "Тексты самих отправленных сообщений и их удаление админами — не хранится; "
    "sent_24h в фактах = сколько своих сообщений аккаунт реально оставил в чате за сутки.",
    "Сессии (.session), токены, API-ключи не выгружаются намеренно.",
]


def _mask_phone(phone: str) -> str:
    p = (phone or "").strip()
    return p[:3] + "***" + p[-2:] if len(p) > 6 else ("***" if p else "")


def _mask_link(link: str) -> str:
    l = (link or "").strip()
    return l[:20] + "…" if len(l) > 20 else l


def _git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _d(obj: Any) -> dict[str, Any]:
    return asdict(obj) if is_dataclass(obj) else dict(obj)


def _account_row(a) -> dict[str, Any]:
    d = _d(a)
    d["phone"] = _mask_phone(d.get("phone", ""))
    d["telethon_session"] = bool(d.get("telethon_session"))
    d["pyrogram_session"] = bool(d.get("pyrogram_session"))
    d["sender_bot_token"] = "set" if d.get("sender_bot_token") else ""
    d["has_sender"] = a.has_sender
    return d


def _chat_row(c) -> dict[str, Any]:
    d = _d(c)
    d["invite_link"] = _mask_link(d.get("invite_link", ""))
    return d


def _post_row(account_label: str, p) -> dict[str, Any]:
    d = _d(p)
    d["account"] = account_label
    d["photo_path"] = Path(d["photo_path"]).name if d.get("photo_path") else ""
    return d


async def _sender_live(accounts: list[Any]) -> list[dict[str, Any]]:
    settings = get_settings()
    api = SenderAPI(settings.sender_api_url, settings.sender_api_key, timeout=15.0)
    rows: list[dict[str, Any]] = []
    try:
        health = await api.health()
    except Exception as e:
        return [{"error": f"Autoposter недоступен: {type(e).__name__}: {e}"}]
    rows.append({"autoposter_health": health})
    for a in accounts:
        sid = (a.sender_account_id or "").strip()
        if not sid:
            continue
        row: dict[str, Any] = {"account": a.label, "sender_id": sid}
        try:
            info = await api.get_account(sid)
            row["live"] = info.get("live")
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"
        try:
            cloak = await api.get_cloak(sid)
            row["cloak_enabled"] = bool(cloak.get("enabled"))
            row["cloak_text_len"] = len(str(cloak.get("text") or ""))
        except Exception as e:
            row["cloak_error"] = f"{type(e).__name__}: {e}"
        rows.append(row)
    return rows


def _chat_summary(an: ChatAnalysis) -> dict[str, Any]:
    chat, plan = an.chat, an.plan
    interval = chat.interval_minutes
    circle = circle_length(interval)
    working = sum(1 for a, s in an.statuses.items() if s == WORKING)
    per_day = 1440.0 / (interval + 1)
    return {
        "chat": chat.title,
        "chat_pk": chat.id,
        "interval_min": interval,
        "slots_assigned": len(an.current),
        "working": working,
        "muted": an.counts.get(MUTED, 0),
        "not_member": an.counts.get(NOT_MEMBER, 0),
        "silent": an.counts.get(SILENT, 0),
        "unknown": an.counts.get(UNKNOWN, 0),
        "gap_nominal_min": round(plan.nominal_gap, 1),
        "gap_real_min": round(plan.gap_before, 1),
        "gap_after_plan_min": round(plan.gap_after, 1),
        "gap_ideal_min": round(circle / max(1, working), 1) if working else None,
        "sent_24h_total": an.sent_24h_total,
        "expected_24h": round(working * per_day),
        "accounts_for_5min": accounts_needed(5, interval),
        "accounts_for_10min": accounts_needed(10, interval),
        "accounts_for_15min": accounts_needed(15, interval),
        "unscheduled": len(an.unscheduled),
        "chat_alive": an.chat_alive,
    }


def find_anomalies(
    *,
    analyses: list[ChatAnalysis],
    accounts: list[Any],
    facts_age: timedelta | None,
    sender_live: list[dict[str, Any]],
    settings_rows: dict[str, Any],
    states: list[Any],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(sev: str, scope: str, code: str, msg: str, hint: str = "") -> None:
        out.append({"severity": sev, "scope": scope, "code": code, "message": msg, "hint": hint})

    s = get_settings()
    if facts_age is None:
        add("high", "system", "no_facts", "Фактов по парам аккаунт×чат нет вообще",
            "Запустите «Собрать факты» или экспорт со свежим сбором")
    elif facts_age > timedelta(hours=12):
        add("medium", "system", "facts_stale",
            f"Факты устарели: {facts_age.total_seconds() / 3600:.1f} ч", "Соберите факты заново")

    for an in analyses:
        chat, plan = an.chat, an.plan
        scope = f"chat:{chat.title}"
        working = [a for a, st in an.statuses.items() if st == WORKING]
        if not working and an.current:
            add("high", scope, "nobody_works",
                f"Ни один из {len(an.current)} назначенных аккаунтов не может писать",
                "Проверьте чат вручную: права, капча, режим «только чтение», слоумод")
            continue
        circle = circle_length(chat.interval_minutes)
        if working:
            ideal = circle / len(working)
            if plan.gap_before > max(ideal * s.rebalance_gap_factor, ideal + 2):
                sev = "high" if plan.gap_before >= 20 else "medium"
                add(sev, scope, "gap_hole",
                    f"Пауза между отправками {plan.gap_before:.0f} мин при идеале {ideal:.1f} "
                    f"(в строю {len(working)} из {len(an.current)} назначенных)",
                    "«Перераспределить по факту» раздвинет оставшихся равномерно")
        dead_weight = len(plan.evict)
        if dead_weight:
            add("medium", scope, "dead_slots",
                f"{dead_weight} слотов у аккаунтов, которые не пишут: "
                f"не в чате {an.counts.get(NOT_MEMBER, 0)}, мут {an.counts.get(MUTED, 0)}, "
                f"молчат {an.counts.get(SILENT, 0)}",
                "Освободятся при перераспределении")
        if an.unscheduled:
            add("medium", scope, "unscheduled",
                f"{len(an.unscheduled)} акк. состоят и могут писать, но расписание не настроено/пустое",
                "Соберётся при следующем перераспределении")
        expected_day = len(working) * 1440 / (chat.interval_minutes + 1)
        if working and an.sent_24h_total < 0.5 * expected_day:
            sev = "high" if an.sent_24h_total == 0 else "medium"
            add(sev, scope, "low_sends",
                f"За 24 ч отправлено {an.sent_24h_total}, по плану ≈ {round(expected_day)}",
                "Сравните с логами: слоумод/антиспам/удаление сообщений админами")
        if chat.interval_minutes < 60 and an.current:
            pos = positions(an.current, chat.interval_minutes)
            if len(pos) != len(set(pos)):
                add("medium", scope, "same_minute",
                    f"Интервал {chat.interval_minutes} мин: у части аккаунтов совпадает минута "
                    "внутри окна → две отправки в одну минуту",
                    "Перераспределение выровняет по окну интервала")

    sender_by = {r.get("account"): r for r in sender_live if r.get("account")}
    for a in accounts:
        scope = f"account:{a.label}"
        if a.dead:
            continue
        if not a.telethon_session:
            add("high", scope, "no_telethon", "Нет Telethon-сессии", "Загрузите .session")
        if not a.has_sender:
            add("medium", scope, "no_sender",
                "Sender не задан → клоакинг (автоответ в ЛС) не работает",
                "Укажите Sender ID или Pyrogram+token")
        live = sender_by.get(a.label)
        if live:
            if live.get("cloak_enabled") is False:
                add("high", scope, "cloak_off", "Клоакинг в Autoposter выключен", "Нажмите Sender → клоакинг")
            if live.get("live") is False:
                add("medium", scope, "sender_down", "Клиент Sender не поднят (live=false)", "Старт аккаунта")
            if live.get("error") or live.get("cloak_error"):
                add("medium", scope, "sender_error", str(live.get("error") or live.get("cloak_error")))
    if not settings_rows.get("cloak_enabled") or not str(settings_rows.get("cloak_text", "")).strip():
        add("medium", "system", "cloak_global_off", "Клоакинг выключен/пуст в настройках Sender")

    # аккаунты, у которых вообще ничего не работает → кандидаты в dead
    per_acc: dict[int, list[str]] = {}
    for an in analyses:
        for aid, st in an.statuses.items():
            per_acc.setdefault(aid, []).append(st)
    for a in accounts:
        sts = per_acc.get(a.id)
        if a.dead or not sts:
            continue
        if all(st in (NOT_MEMBER, MUTED, SILENT) for st in sts):
            add("high", f"account:{a.label}", "useless",
                "Ни в одном чате каталога не работает", "Кандидат в dead-режим или на замену")
    abandoned = sum(1 for st in states if st.status == "abandoned")
    if abandoned:
        add("low", "system", "abandoned", f"{abandoned} пар «аккаунт×чат» в статусе abandoned (до начала недели)")
    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda r: (order.get(r["severity"], 3), r["scope"]))
    return out


async def build_report(
    store: Store,
    *,
    fresh: bool = False,
    include_live: bool = False,
) -> dict[str, Any]:
    """Собрать все данные в один словарь. fresh — перед этим обновить факты."""
    settings = get_settings()
    if fresh:
        from app.jobs.facts import run_collect_facts

        await run_collect_facts(store, None, None, job_kind="collect_facts")
    now = datetime.now(timezone.utc)
    accounts = await store.list_accounts()
    chats = await store.list_chats()
    schedule_chats = [c for c in chats if c.enabled and c.is_schedule]
    live_accounts = {a.id: a for a in accounts if a.telethon_session and not a.is_dead}
    facts = await store.list_facts()
    states = await store.list_setup_states()
    analyses, _a, _f = await analyze_state(store, live_accounts, schedule_chats, facts, now=now)

    from app.jobs.balance import _facts_age

    age = _facts_age(facts)
    sender_live = await _sender_live(accounts) if (fresh or include_live) else []
    ss = await store.sender_settings()
    online = await store.online_ping_settings()
    labels = {a.id: a.label for a in accounts}
    titles = {c.id: c.title for c in chats}
    state_by = {(s.account_id, s.chat_pk): s for s in states}
    fact_by = {(f.account_id, f.chat_pk): f for f in facts}

    slots = []
    for c in chats:
        for sl in await store.slots_for_chat(c.id):
            slots.append({"chat": c.title, "chat_pk": c.id, "account": sl.account_label,
                          "account_id": sl.account_id, "minute": sl.start_minute})

    matrix = []
    for an in analyses:
        for aid, acc in live_accounts.items():
            st = state_by.get((aid, an.chat.id))
            f = fact_by.get((aid, an.chat.id))
            matrix.append({
                "chat": an.chat.title, "account": acc.label,
                "slot_minute": an.current.get(aid),
                "status": an.statuses.get(aid, ""),
                "in_roster": aid in an.plan.roster,
                "setup_status": st.status if st else "",
                "setup_fail_count": st.fail_count if st else 0,
                "setup_last_error": st.last_error if st else "",
                "ok_since": st.first_ok_at if st else "",
                "member": f.member if f else None,
                "can_send": f.can_send if f else None,
                "mute_until": f.mute_until if f else "",
                "sent_24h": f.sent_24h if f else None,
                "last_sent_at": f.last_sent_at if f else "",
                "scheduled_count": f.scheduled_count if f else None,
                "fact_error": f.error if f else "",
                "fact_checked_at": f.checked_at if f else "",
            })

    chat_summary = [_chat_summary(an) for an in analyses]
    account_summary = []
    for a in accounts:
        mine = [m for m in matrix if m["account"] == a.label]
        account_summary.append({
            "account": a.label, "id": a.id, "dead": bool(a.is_dead),
            "premium": bool(a.is_premium), "has_sender": a.has_sender,
            "telethon": bool(a.telethon_session), "status": a.status, "last_error": a.last_error,
            "slots": sum(1 for m in mine if m["slot_minute"] is not None),
            "working_chats": sum(1 for m in mine if m["status"] == WORKING),
            "not_member": sum(1 for m in mine if m["status"] == NOT_MEMBER),
            "muted": sum(1 for m in mine if m["status"] == MUTED),
            "silent": sum(1 for m in mine if m["status"] == SILENT),
            "unknown": sum(1 for m in mine if m["status"] == UNKNOWN),
            "sent_24h_total": sum((m["sent_24h"] or 0) for m in mine),
        })

    settings_rows = {
        "cloak_enabled": ss.cloak_enabled, "cloak_text": ss.cloak_text,
    }
    anomalies = find_anomalies(
        analyses=analyses, accounts=accounts, facts_age=age,
        sender_live=sender_live, settings_rows=settings_rows, states=states,
    )

    jobs = await store.raw_rows(
        "SELECT j.*, COALESCE(a.label,'') AS account FROM jobs j "
        "LEFT JOIN accounts a ON a.id=j.account_id ORDER BY j.id DESC LIMIT ?", (MAX_JOBS,))
    logs = await store.raw_rows(
        "SELECT l.id, l.job_id, l.created_at, l.level, l.message, j.kind FROM job_logs l "
        "LEFT JOIN jobs j ON j.id=l.job_id ORDER BY l.id DESC LIMIT ?", (MAX_LOGS,))
    posts = []
    for a in accounts:
        for lang in ("ru", "en", "ru_short", "en_short"):
            p = await store.get_post(a.id, lang)
            if p.has_compose or p.has_text_link or p.has_photo_link:
                posts.append(_post_row(a.label, p))

    all_settings = await store.raw_rows("SELECT key, value FROM settings ORDER BY key")
    return {
        "meta": {
            "generated_at_utc": now.isoformat(timespec="seconds"),
            "generated_at_local": datetime.now(settings.tz).isoformat(timespec="seconds"),
            "timezone": settings.timezone,
            "git_rev": _git_rev(),
            "python": platform.python_version(),
            "fresh_facts": fresh,
            "facts_age_hours": None if age is None else round(age.total_seconds() / 3600, 2),
            "maintenance_busy": maintenance_lock.locked(),
            "running_jobs": runtime.running_keys(),
            "counts": {
                "accounts": len(accounts), "accounts_dead": sum(1 for a in accounts if a.is_dead),
                "chats": len(chats), "schedule_chats": len(schedule_chats),
                "slots": len(slots), "facts": len(facts), "jobs_exported": len(jobs),
                "logs_exported": len(logs),
            },
            "truncated": {"jobs": len(jobs) >= MAX_JOBS, "logs": len(logs) >= MAX_LOGS},
        },
        "config": {
            k: getattr(settings, k) for k in (
                "timezone", "start_hour", "repeat_period", "setup_parallel",
                "schedule_pause_sec", "setup_retry_days", "setup_max_attempts",
                "setup_retry_hour", "nonpremium_reschedule_hour", "nonpremium_extra_hours",
                "dead_interval_minutes", "auto_rebalance", "rebalance_every_hours",
                "rebalance_gap_factor", "facts_max_age_hours", "facts_fresh_minutes",
                "silent_after_hours",
            )
        },
        "settings_table": all_settings,
        "sender_settings": _d(ss),
        "online_settings": _d(online),
        "accounts": [_account_row(a) for a in accounts],
        "chats": [_chat_row(c) for c in chats],
        "posts": posts,
        "slots": slots,
        "setup_states": [_d(s) for s in states],
        "facts": [_d(f) for f in facts],
        "matrix": matrix,
        "chat_summary": chat_summary,
        "account_summary": account_summary,
        "anomalies": anomalies,
        "sender_live": sender_live,
        "jobs": jobs,
        "job_logs": logs,
        "not_available": NOT_AVAILABLE,
        "plan_preview": {
            an.chat.title: {
                "roster": [labels.get(a, str(a)) for a in an.plan.roster],
                "evict": {labels.get(a, str(a)): r for a, r in an.plan.evict.items()},
                "added": [labels.get(a, str(a)) for a in an.plan.added],
                "rebalanced": an.plan.rebalanced,
                "gap_before": an.plan.gap_before,
                "gap_after": an.plan.gap_after,
                "skipped": an.plan.skipped_reason,
            } for an in analyses
        },
        "_titles": titles,
    }


def render_summary(data: dict[str, Any]) -> str:
    m = data["meta"]
    lines = [
        "# Marketing — выгрузка состояния",
        "",
        f"Собрано: {m['generated_at_local']} ({m['timezone']}) · версия {m['git_rev'] or '—'} · "
        f"факты: {'свежие' if m['fresh_facts'] else 'из базы'}, "
        f"возраст {m['facts_age_hours'] if m['facts_age_hours'] is not None else '—'} ч",
        f"Аккаунтов {m['counts']['accounts']} (dead {m['counts']['accounts_dead']}), "
        f"чатов {m['counts']['schedule_chats']}, слотов {m['counts']['slots']}, "
        f"задач {m['counts']['jobs_exported']}, логов {m['counts']['logs_exported']}",
        f"Сейчас идёт: {', '.join(m['running_jobs']) or 'ничего'}"
        + (" · занята суточная пересборка/перераспределение" if m["maintenance_busy"] else ""),
        "",
        "## Что бросается в глаза",
        "",
    ]
    anomalies = data["anomalies"]
    if not anomalies:
        lines.append("Аномалий не найдено.")
    for r in anomalies[:60]:
        hint = f" → {r['hint']}" if r.get("hint") else ""
        lines.append(f"- **{r['severity']}** `{r['scope']}` {r['message']}{hint}")
    if len(anomalies) > 60:
        lines.append(f"- … ещё {len(anomalies) - 60} в report.json → anomalies")

    lines += ["", "## Чаты", "",
              "| чат | интервал | назнач. | в строю | мут | не в чате | молчат | пауза факт→после | идеал | отпр. 24ч / ожид. |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for c in data["chat_summary"]:
        lines.append(
            f"| {c['chat']} | {c['interval_min']} | {c['slots_assigned']} | {c['working']} | "
            f"{c['muted']} | {c['not_member']} | {c['silent']} | "
            f"{c['gap_real_min']}→{c['gap_after_plan_min']} | {c['gap_ideal_min']} | "
            f"{c['sent_24h_total']} / {c['expected_24h']} |"
        )
    lines += ["", "## Аккаунты", "",
              "| аккаунт | prem | dead | sender | чатов работает | не в чате | мут | молчит | отпр. 24ч | ошибка |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for a in data["account_summary"]:
        lines.append(
            f"| {a['account']} | {'да' if a['premium'] else ''} | {'💀' if a['dead'] else ''} | "
            f"{'да' if a['has_sender'] else '**нет**'} | {a['working_chats']} | {a['not_member']} | "
            f"{a['muted']} | {a['silent']} | {a['sent_24h_total']} | {(a['last_error'] or '')[:60]} |"
        )
    lines += ["", "## Чего нет в этой выгрузке", ""] + [f"- {x}" for x in data["not_available"]]
    lines += [
        "", "## Файлы", "",
        "- `report.json` — всё сразу (источник правды)",
        "- `matrix.csv` — аккаунт × чат: слот, статус, факты, ошибки",
        "- `accounts.csv`, `chats.csv`, `slots.csv`, `setup_states.csv`, `facts.csv`, `posts.csv`",
        "- `jobs.csv`, `job_logs.csv` — история задач и логи (новые сверху)",
        "- `anomalies.csv` — найденные проблемы с подсказками",
    ]
    return "\n".join(lines) + "\n"


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return buf.getvalue().encode("utf-8-sig")


def build_zip(data: dict[str, Any]) -> bytes:
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SUMMARY.md", render_summary(data))
        zf.writestr("report.json", json.dumps(payload, ensure_ascii=False, indent=1, default=str))
        for name, key in (
            ("accounts.csv", "account_summary"), ("accounts_full.csv", "accounts"),
            ("chats.csv", "chat_summary"), ("chats_full.csv", "chats"),
            ("matrix.csv", "matrix"), ("slots.csv", "slots"),
            ("setup_states.csv", "setup_states"), ("facts.csv", "facts"),
            ("posts.csv", "posts"), ("anomalies.csv", "anomalies"),
            ("jobs.csv", "jobs"), ("job_logs.csv", "job_logs"),
        ):
            blob = _csv_bytes(data.get(key) or [])
            if blob:
                zf.writestr(name, blob)
    return out.getvalue()


async def send_report(
    store: Store, bot: Bot, chat_id: int, *, fresh: bool = False
) -> str:
    """Собрать и отправить ZIP в Telegram. Возвращает краткий текст."""
    from aiogram.types import BufferedInputFile

    data = await build_report(store, fresh=fresh, include_live=True)
    blob = build_zip(data)
    stamp = datetime.now(get_settings().tz).strftime("%Y%m%d_%H%M")
    head = render_summary(data).split("## Чаты")[0]
    await bot.send_document(
        chat_id,
        BufferedInputFile(blob, filename=f"marketing_report_{stamp}.zip"),
        caption=head[:1000],
    )
    return head
