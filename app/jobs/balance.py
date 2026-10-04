"""Умное перераспределение слотов: по фактически работающим аккаунтам.

Раньше сетка минут раздавалась всем аккаунтам × всем чатам. Аккаунты, которые
не состоят в чате / в муте / молчат, занимали минуты и оставляли «дыры» —
вместо пауз 5–8 мин получались 40+. Здесь: берём факты → считаем, кто реально
пишет → раздвигаем их равномерно (с минимальными сдвигами) → пересобираем
расписание только у тех, у кого слот изменился.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.config import get_settings
from app.jobs import LogSink, maintenance_lock, runtime
from app.jobs.facts import run_collect_facts
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.models import Account, Chat
from app.store import Store
from app.utils.schedule import posts_count_for_interval
from app.utils.balance import (
    MUTED,
    NOT_MEMBER,
    SILENT,
    UNKNOWN,
    WORKING,
    ChatPlan,
    PairFact,
    circle_length,
    classify_pair,
    plan_chat,
)

log = logging.getLogger("marketing.balance")

JOB_KIND = "smart_rebalance"


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _facts_age(facts: list[PairFact]) -> timedelta | None:
    newest = None
    for f in facts:
        dt = _parse(f.checked_at)
        if dt and (newest is None or dt > newest):
            newest = dt
    return None if newest is None else datetime.now(timezone.utc) - newest


def _fmt_plan(title: str, interval: int, plan: ChatPlan, counts: dict[str, int]) -> str:
    circle = circle_length(interval)
    parts = []
    for key, label in (
        (NOT_MEMBER, "не в чате"),
        (MUTED, "мут"),
        (SILENT, "молчат"),
        (UNKNOWN, "нет данных"),
    ):
        if counts.get(key):
            parts.append(f"{label} {counts[key]}")
    why = f" ({', '.join(parts)})" if parts else ""
    head = (
        f"«{title}»: в строю {len(plan.roster)}{why}; "
        f"пауза {plan.gap_before:.0f}→{plan.gap_after:.0f} мин "
        f"(идеал {circle / max(1, len(plan.roster)):.1f})"
    )
    if plan.skipped_reason:
        return f"{head} — {plan.skipped_reason}"
    notes = []
    if plan.evict:
        notes.append(f"освободил слотов {len(plan.evict)}")
    if plan.added:
        notes.append(f"добавил {len(plan.added)}")
    if plan.rebalanced:
        notes.append("раздвинул")
    if plan.changed:
        notes.append(f"пересобрать {len(plan.changed)}")
    return head + (" | " + ", ".join(notes) if notes else " | ок")


@dataclass
class ChatAnalysis:
    chat: Chat
    index: int
    current: dict[int, int]  # account_id → минута (как сейчас в БД)
    statuses: dict[int, str]  # account_id → working/muted/not_member/…
    unscheduled: list[int]  # нужна (пере)сборка расписания
    counts: dict[str, int]  # сколько аккаунтов в каждом нерабочем статусе
    plan: ChatPlan
    chat_alive: bool
    sent_24h_total: int
    facts: list[PairFact] = field(default_factory=list)


async def analyze_state(
    store: Store,
    accounts: dict[int, Account],
    chats: list[Chat],
    facts: list[PairFact],
    *,
    now: datetime | None = None,
) -> tuple[list[ChatAnalysis], dict[int, Account], list[PairFact]]:
    """Один и тот же разбор «что реально работает» для перераспределения и для отчёта."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    fact_by = {(f.account_id, f.chat_pk): f for f in facts}
    states = {(s.account_id, s.chat_pk): s for s in await store.list_setup_states()}
    max_age = timedelta(hours=settings.facts_max_age_hours)
    silent_after = timedelta(hours=settings.silent_after_hours)
    out: list[ChatAnalysis] = []
    for idx, chat in enumerate(chats):
        slots = await store.slots_for_chat(chat.id)
        current = {s.account_id: s.start_minute for s in slots}
        chat_facts = [f for (aid, cpk), f in fact_by.items() if cpk == chat.id]
        chat_alive = sum(1 for f in chat_facts if (f.sent_24h or 0) > 0) >= 3

        statuses: dict[int, str] = {}
        unscheduled: list[int] = []
        expected = posts_count_for_interval(chat.interval_minutes)
        for aid in accounts:
            st = states.get((aid, chat.id))
            f = fact_by.get((aid, chat.id))
            status = classify_pair(
                f,
                state_ok=bool(st and st.status == "ok"),
                state_ok_since=st.first_ok_at if st else "",
                now=now,
                max_age=max_age,
                silent_after=silent_after,
                chat_alive=chat_alive,
            )
            statuses[aid] = status
            acc = accounts.get(aid)
            # Premium держит расписание на repeat: должно стоять ровно полные сутки.
            # Меньше (старые 23 слота с дырой ~40 мин) → пересобрать один раз.
            need = expected if (acc and acc.has_premium) else max(1, expected // 2)
            low = (
                f is not None
                and f.scheduled_count is not None
                and f.scheduled_count < need
            )
            if status == WORKING and (not (st and st.status == "ok") or low):
                unscheduled.append(aid)
        # слоты мёртвых/удалённых/без сессии — всегда освобождаем
        for aid in current:
            if aid not in accounts:
                statuses[aid] = NOT_MEMBER

        plan = plan_chat(
            interval_minutes=chat.interval_minutes,
            current=current,
            statuses=statuses,
            unscheduled=unscheduled,
            chat_index=idx,
            chats_total=len(chats),
            factor=settings.rebalance_gap_factor,
        )
        counts: dict[str, int] = defaultdict(int)
        for aid, status in statuses.items():
            if aid in accounts and status != WORKING:
                counts[status] += 1
        out.append(
            ChatAnalysis(
                chat=chat,
                index=idx,
                current=current,
                statuses=statuses,
                unscheduled=unscheduled,
                counts=dict(counts),
                plan=plan,
                chat_alive=chat_alive,
                sent_24h_total=sum((f.sent_24h or 0) for f in chat_facts),
                facts=chat_facts,
            )
        )
    return out, accounts, facts


async def run_smart_rebalance(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    collect: bool = True,
    dry_run: bool = False,
    notify: str = "changes",  # always | changes | never
) -> str:
    """Перераспределить слоты по факту. dry_run — только показать план."""
    from app.jobs.setup import run_setup_chats_only

    if maintenance_lock.locked():
        return "Занято: идёт суточная пересборка или другое перераспределение"
    settings = get_settings()
    async with maintenance_lock:
        job = await store.create_job(JOB_KIND, None)
        sink = LogSink(store, job.id, None, None)
        try:
            chats = [c for c in await store.list_chats(enabled_only=True) if c.is_schedule]
            accounts = {
                a.id: a
                for a in await store.list_accounts()
                if a.telethon_session and not a.is_dead
            }
            if not chats or not accounts:
                report = "Нечего перераспределять (нет чатов или аккаунтов)"
                await store.finish_job(job.id, "done", report)
                return report

            facts = await store.list_facts()
            age = _facts_age(facts)
            if collect and (
                age is None or age > timedelta(minutes=settings.facts_fresh_minutes)
            ):
                await sink.emit("Собираю факты (состоит/права/отправки)…", notify=False)
                await run_collect_facts(store, None, None, only_account_ids=set(accounts))
                facts = await store.list_facts()
            if not facts:
                report = "Нет фактов — нечего считать (соберите факты)"
                await store.finish_job(job.id, "error", report)
                return report

            analyses, _acc, _facts = await analyze_state(store, accounts, chats, facts)
            lines: list[str] = []
            by_account: dict[int, list[int]] = defaultdict(list)
            total_changed = total_evicted = total_added = 0

            for an in analyses:
                chat, plan, current = an.chat, an.plan, an.current
                lines.append(_fmt_plan(chat.title, chat.interval_minutes, plan, an.counts))

                touched = bool(plan.evict or plan.added or plan.rebalanced or plan.changed)
                if not touched or plan.skipped_reason:
                    continue
                total_evicted += len(plan.evict)
                total_added += len(plan.added)
                if dry_run:
                    total_changed += len(plan.changed)
                    continue
                # убрать всё и записать заново — иначе UNIQUE(chat, minute) спотыкается о сдвиги
                for aid in list(current):
                    await store.delete_slot(chat.id, aid)
                for aid, minute in plan.assign.items():
                    try:
                        await store.set_slot(chat.id, aid, minute)
                    except ValueError:
                        log.warning("slot conflict chat=%s acc=%s min=%s", chat.id, aid, minute)
                for aid in plan.changed:
                    by_account[aid].append(chat.id)
                    total_changed += 1

            if not dry_run and by_account:
                batch_size, batch_pause = setup_parallel_defaults()
                await sink.emit(
                    f"Пересобираю расписание: {len(by_account)} акк., "
                    f"{total_changed} пар…",
                    notify=False,
                )

                async def _one(item: tuple[int, list[int]]) -> bool:
                    aid, pks = item
                    if runtime.cancelled(JOB_KIND, 0):
                        return False
                    try:
                        await run_setup_chats_only(
                            store,
                            aid,
                            pks,
                            None,
                            None,
                            job_kind="rebalance",
                            skip_abandoned=False,
                        )
                        return True
                    except Exception as e:
                        await sink.emit(
                            f"✗ acc#{aid}: {type(e).__name__}: {e}", "error", notify=False
                        )
                        return False

                results = await map_batches(
                    list(by_account.items()),
                    _one,
                    batch_size=batch_size,
                    batch_pause=batch_pause,
                    is_cancelled=lambda: runtime.cancelled(JOB_KIND, 0),
                )
                bad = sum(1 for r in results if r is not True)
                lines.append(f"Пересборка: ok={len(results) - bad}, ошибок={bad}")

            head = (
                ("ПЛАН (ничего не применено)\n" if dry_run else "")
                + f"Перераспределение по факту | чатов {len(chats)}, аккаунтов {len(accounts)} | "
                f"освобождено слотов {total_evicted}, добавлено {total_added}, "
                f"пересобрать пар {total_changed}"
            )
            report = head + "\n" + "\n".join(lines)
            await store.finish_job(job.id, "done", report)
            changed_any = bool(total_evicted or total_added or total_changed)
            if bot and admin_chat_id and (
                notify == "always" or (notify == "changes" and changed_any)
            ):
                try:
                    await bot.send_message(admin_chat_id, report[:3900])
                except Exception:
                    pass
            return report
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            await store.finish_job(job.id, "error", err)
            log.exception("smart rebalance failed")
            return f"Ошибка перераспределения: {err}"
