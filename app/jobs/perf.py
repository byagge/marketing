"""Результативность аккаунтов: кто не приносит клиентов — в список «для переоформления»."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

from aiogram import Bot

from app.config import get_settings
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.models import Account, AccountPerf
from app.store import Store
from app.tg.client import telethon_client
from app.tg.dmstats import collect_dm_stats
from app.utils.perf import (
    VERDICT_TEXT,
    age_days,
    apply_measurement,
    check_due,
    mark_redesigned,
    minus_days,
)


@dataclass
class PerfEvent:
    account: Account
    perf: AccountPerf
    kind: str  # flagged | reminder | recovered


def describe_stats(perf: AccountPerf) -> str:
    cloak = "—" if perf.cloak_n < 0 else str(perf.cloak_n)
    return (
        f"написали {perf.wrote_n} чел. за {perf.window_days} дн. "
        f"(первыми — {perf.new_n}), клоакинг сработал: {cloak}"
    )


async def perf_step(
    store: Store,
    client,
    acc: Account,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> PerfEvent | None:
    """
    Измерить аккаунт (раз в perf_check_hours): сколько людей пишут ему в личку и сколько раз
    ответил клоакинг. Новый / «не оценивать» — пропуск. Возвращает событие, если пора сообщить.
    """
    cfg = get_settings()
    moment = now or datetime.now(timezone.utc)
    prev = await store.get_perf(acc.id)
    if prev.skip and not force:
        return None
    if not force:
        if age_days(acc.created_at, moment) < cfg.perf_min_age_days:
            return None
        if not check_due(prev, moment, cfg.perf_check_hours):
            return None
    ss = await store.sender_settings()
    cloak_text = ss.cloak_text if ss.cloak_enabled else ""
    try:
        stats = await collect_dm_stats(
            client,
            minus_days(moment, cfg.perf_window_days),
            cloak_text=cloak_text,
            max_dialogs=cfg.perf_max_dialogs,
        )
    except Exception:
        # не повторяем каждый проход: следующая попытка через perf_check_hours
        prev.checked_at = moment.isoformat(timespec="seconds")
        await store.save_perf(prev)
        return None
    outcome = apply_measurement(
        prev,
        new_n=stats.new_n,
        wrote_n=stats.wrote_n,
        cloak_n=stats.cloak_n,
        scanned=stats.scanned,
        truncated=stats.truncated,
        window_days=cfg.perf_window_days,
        now=moment,
        low_threshold=cfg.perf_low_threshold,
        grace_days=cfg.perf_grace_days,
        renotify_days=cfg.perf_renotify_days,
    )
    saved = await store.save_perf(outcome.perf)
    if outcome.event:
        return PerfEvent(acc, saved, outcome.event)
    return None


async def mark_account_redesigned(store: Store, account_id: int, now: datetime | None = None) -> AccountPerf:
    prev = await store.get_perf(account_id)
    return await store.save_perf(mark_redesigned(prev, now or datetime.now(timezone.utc)))


async def toggle_account_skip(store: Store, account_id: int) -> AccountPerf:
    prev = await store.get_perf(account_id)
    prev.skip = 0 if prev.skip else 1
    if prev.skip:  # не оцениваем — и из списка убираем
        prev.flagged_at = prev.notified_at = ""
    return await store.save_perf(prev)


async def run_perf_scan(store: Store, bot: Bot | None = None, admin_chat_id: int | None = None) -> str:
    """Принудительно измерить все Telethon-аккаунты (кнопка «Проверить сейчас»)."""
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    size, pause = setup_parallel_defaults()

    async def _one(acc: Account) -> PerfEvent | None:
        async with telethon_client(acc.telethon_session) as client:
            return await perf_step(store, client, acc, force=True)

    results = await map_batches(accounts, _one, batch_size=min(size, 4), batch_pause=pause)
    errors = sum(1 for r in results if isinstance(r, BaseException))
    flagged = len([p for p in await store.list_perf() if p.flagged])
    return f"Проверено аккаунтов: {len(accounts) - errors}, ошибок: {errors}. Для переоформления: {flagged}."


def _label(acc: Account | None, account_id: int) -> str:
    if acc is None:
        return f"#{account_id}"
    return acc.label + (f" (@{acc.username})" if acc.username else "")


async def redesign_candidates(store: Store) -> list[tuple[Account, AccountPerf]]:
    """Аккаунты в списке «для переоформления»: сначала те, кого дольше всех ждём."""
    accounts = {a.id: a for a in await store.list_accounts()}
    items = [
        (accounts[p.account_id], p)
        for p in await store.list_perf()
        if p.flagged and p.account_id in accounts
    ]
    return sorted(items, key=lambda it: it[1].flagged_at)


async def redesign_report_html(store: Store) -> str:
    """Раздел отчёта «Аккаунты для переоформления»."""
    cfg = get_settings()
    accounts = {a.id: a for a in await store.list_accounts()}
    perfs = {p.account_id: p for p in await store.list_perf()}
    flagged = await redesign_candidates(store)
    lines = [f"🎨 <b>Аккаунты для переоформления</b> — {len(flagged)}\n"]
    if flagged:
        for acc, p in flagged[:10]:
            lines.append(
                f"· <b>{escape(_label(acc, acc.id))}</b> — {escape(VERDICT_TEXT.get(p.verdict, p.verdict))}\n"
                f"   {escape(describe_stats(p))}\n"
                f"   в списке с {escape(p.flagged_at[:10])}"
            )
        if len(flagged) > 10:
            lines.append(f"… и ещё {len(flagged) - 10} (после обработки первых появятся)")
    else:
        lines.append("<i>Сейчас все измеренные аккаунты приносят клиентов.</i>")
    graced = [a for a in accounts.values() if perfs.get(a.id) and perfs[a.id].verdict == "grace"]
    skipped = [a for a in accounts.values() if perfs.get(a.id) and perfs[a.id].skip]
    unmeasured = [
        a for a in accounts.values()
        if a.telethon_session and not (perfs.get(a.id) and perfs[a.id].measured)
    ]
    lines.append("")
    if graced:
        lines.append(f"🔄 Переоформлены, ждём результат ({cfg.perf_grace_days} дн.): "
                     + escape(", ".join(a.label for a in graced[:15])))
    if skipped:
        lines.append("🚫 Не оцениваем: " + escape(", ".join(a.label for a in skipped[:15])))
    if unmeasured:
        lines.append(f"⏳ Ещё не измерены (моложе {cfg.perf_min_age_days} дн. или очередь): {len(unmeasured)}")
    lines.append(
        f"\n<i>Оценка: за {cfg.perf_window_days} дн. написали меньше {cfg.perf_low_threshold} человек — "
        f"«пишут мало», ноль — «никто не пишет».</i>"
    )
    return "\n".join(lines)
