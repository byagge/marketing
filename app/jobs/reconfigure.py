"""Пересборка минутной таблицы + массовая перенастройка аккаунтов."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from aiogram import Bot

from app.jobs import LogSink, runtime
from app.store import Store
from app.utils.minutes import TABLE_PERIOD, plan_chat_minutes, suggest_minute

# Пачка setup: не больше 5 параллельно — иначе FloodWait / бан.
SETUP_BATCH_SIZE = 5
SETUP_BATCH_PAUSE_SEC = 6.0


async def rebalance_minute_table(
    store: Store,
    *,
    only_assigned: bool = False,
) -> dict[str, int]:
    """
    Пересобрать слоты schedule-чатов равномерно по часу (0–59).

    only_assigned=False — все аккаунты × все чаты (полная сетка).
    only_assigned=True  — только уже назначенные пары, минуты выровнять.
    """
    chats = [c for c in await store.list_chats(kind="schedule") if c.enabled]
    accounts = await store.list_accounts()
    if not chats:
        return {"chats": 0, "accounts": len(accounts), "slots": 0, "mode": "empty"}

    total = len(chats)
    slots_written = 0
    period = TABLE_PERIOD

    if only_assigned:
        for idx, chat in enumerate(chats):
            existing = await store.slots_for_chat(chat.id)
            if not existing:
                continue
            # стабильный порядок: старая минута → id
            existing = sorted(
                existing,
                key=lambda s: (int(s.start_minute), int(s.account_id)),
            )
            account_ids = [s.account_id for s in existing]
            minutes = plan_chat_minutes(
                period,
                len(account_ids),
                chat_index=idx,
                chats_total=total,
            )
            for s in existing:
                await store.delete_slot(chat.id, s.account_id)
            for aid, minute in zip(account_ids, minutes):
                await store.set_slot(chat.id, aid, minute)
                slots_written += 1
        return {
            "chats": len(chats),
            "accounts": len(accounts),
            "slots": slots_written,
            "mode": "assigned",
        }

    # Полная сетка: каждый аккаунт во каждый chat (до 60 слотов).
    for chat in chats:
        for slot in await store.slots_for_chat(chat.id):
            await store.delete_slot(chat.id, slot.account_id)

    for idx, chat in enumerate(chats):
        n = min(len(accounts), period)
        minutes = plan_chat_minutes(
            period,
            n,
            chat_index=idx,
            chats_total=total,
        )
        occupied: list[int] = list(minutes)
        assigned = list(zip(accounts[:n], minutes))
        for account in accounts[n:]:
            # больше 60 аккаунтов — некуда; пропускаем
            try:
                m = suggest_minute(period, occupied)
            except Exception:
                break
            occupied.append(m)
            assigned.append((account, m))
        for account, minute in assigned:
            await store.set_slot(chat.id, account.id, minute)
            slots_written += 1

    return {
        "chats": len(chats),
        "accounts": len(accounts),
        "slots": slots_written,
        "mode": "full",
    }


async def _run_setup_batch(
    store: Store,
    bot: Bot,
    admin_chat_id: int,
    account_ids: list[int],
    chat_pks_by_account: dict[int, list[int]],
    log: LogSink,
    *,
    parent_kind: str,
) -> tuple[int, int]:
    """Запустить setup только schedule-чатов для пачки аккаунтов параллельно."""
    from app.jobs.setup import run_setup_chats_only

    ok = err = 0

    async def _one(aid: int) -> bool:
        if runtime.cancelled(parent_kind, 0):
            return False
        pks = chat_pks_by_account.get(aid) or []
        if not pks:
            return True
        if runtime.is_running("setup", aid) or runtime.is_running("fix_minutes", aid):
            await log.emit(f"Пропуск acc#{aid}: уже идёт setup", "error")
            return False
        try:
            buckets = await run_setup_chats_only(
                store,
                aid,
                pks,
                bot,
                admin_chat_id,
                job_kind="fix_minutes",
                skip_abandoned=False,
            )
            n_ok = len(buckets.get("ok") or [])
            await log.emit(f"✓ acc#{aid}: schedule ok={n_ok}")
            return True
        except Exception as e:
            await log.emit(f"✗ acc#{aid}: {type(e).__name__}: {e}", "error")
            return False

    results = await asyncio.gather(*[_one(aid) for aid in account_ids], return_exceptions=True)
    for r in results:
        if r is True:
            ok += 1
        else:
            err += 1
    return ok, err


async def run_fix_assigned_minutes(
    store: Store,
    bot: Bot,
    admin_chat_id: int,
    *,
    batch_size: int = SETUP_BATCH_SIZE,
    batch_pause: float = SETUP_BATCH_PAUSE_SEC,
) -> str:
    """
    1) Выровнять минуты у уже назначенных слотов (равные промежутки по часу)
    2) Переназначить schedule в Telegram пачками по ``batch_size`` аккаунтов
    """
    job = await store.create_job("fix_minutes", None)
    log = LogSink(store, job.id, bot, admin_chat_id)
    lines: list[str] = []

    try:
        await log.emit(
            "Выравниваю минуты назначенных слотов по часу (равные промежутки)…"
        )
        stats = await rebalance_minute_table(store, only_assigned=True)
        msg = (
            f"Таблица: {stats['slots']} слотов, "
            f"{stats['chats']} чатов (mode={stats.get('mode')})"
        )
        lines.append(msg)
        await log.emit(msg)

        slots = await store.all_slots()
        schedule_ids = {
            c.id for c in await store.list_chats(kind="schedule") if c.enabled
        }
        chat_pks_by_account: dict[int, list[int]] = defaultdict(list)
        for s in slots:
            if s.chat_pk not in schedule_ids:
                continue
            chat_pks_by_account[s.account_id].append(s.chat_pk)

        account_ids = sorted(chat_pks_by_account.keys())
        if not account_ids:
            report = "Нет назначенных слотов — нечего править"
            await store.finish_job(job.id, "done", report)
            await log.emit(report)
            return report

        await log.emit(
            f"Переназначаю schedule: {len(account_ids)} акк., "
            f"пачки по {batch_size} (пауза {batch_pause:.0f}с)…"
        )
        ok_total = err_total = 0
        size = max(1, int(batch_size))
        for i in range(0, len(account_ids), size):
            if runtime.cancelled("fix_minutes", 0):
                raise RuntimeError("Остановлено")
            batch = account_ids[i : i + size]
            labels = ", ".join(str(a) for a in batch)
            await log.emit(f"Пачка {i // size + 1}: {labels}")
            ok, err = await _run_setup_batch(
                store,
                bot,
                admin_chat_id,
                batch,
                dict(chat_pks_by_account),
                log,
                parent_kind="fix_minutes",
            )
            ok_total += ok
            err_total += err
            if i + size < len(account_ids):
                await asyncio.sleep(max(0.0, float(batch_pause)))

        lines.append(f"Schedule: ok={ok_total}, ошибок={err_total}")
        report = "\n".join(lines)
        await store.finish_job(job.id, "done", report)
        await log.emit("Выравнивание минут завершено.\n" + report)
        return report
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        await store.finish_job(job.id, "error", err)
        await log.emit(f"Ошибка выравнивания: {err}", "error")
        return err


async def run_reconfigure_all(
    store: Store,
    bot: Bot,
    admin_chat_id: int,
    *,
    rebalance: bool = True,
    setup_accounts: bool = True,
    batch_size: int = SETUP_BATCH_SIZE,
    batch_pause: float = SETUP_BATCH_PAUSE_SEC,
) -> str:
    """
    1) Пересобрать таблицу минут равномерно (полная сетка)
    2) Перенастроить аккаунты пачками по batch_size
    """
    from app.jobs.setup import run_setup

    job = await store.create_job("reconfigure_all", None)
    log = LogSink(store, job.id, bot, admin_chat_id)
    lines: list[str] = []

    try:
        if rebalance:
            await log.emit("Пересобираю минутную таблицу равномерно по часу…")
            stats = await rebalance_minute_table(store, only_assigned=False)
            msg = (
                f"Таблица: {stats['slots']} слотов "
                f"({stats['accounts']} акк. × {stats['chats']} чатов)"
            )
            lines.append(msg)
            await log.emit(msg)

        if setup_accounts:
            accounts = await store.list_accounts()
            await log.emit(
                f"Перенастраиваю аккаунты пачками по {batch_size}: {len(accounts)}"
            )
            ok = err = 0
            size = max(1, int(batch_size))
            for i in range(0, len(accounts), size):
                if runtime.cancelled("reconfigure_all", 0):
                    raise RuntimeError("Остановлено")
                batch = accounts[i : i + size]
                await log.emit(
                    f"Пачка {i // size + 1}: " + ", ".join(a.label for a in batch)
                )

                async def _one(acc):
                    if runtime.is_running("setup", acc.id):
                        await log.emit(f"Пропуск {acc.label}: уже идёт setup", "error")
                        return False
                    try:
                        done = runtime.spawn(
                            "setup",
                            acc.id,
                            run_setup(store, acc.id, bot, admin_chat_id),
                        )
                        await done
                    except Exception as e:
                        await log.emit(
                            f"✗ {acc.label}: {type(e).__name__}: {e}",
                            "error",
                        )
                        return False
                    acc2 = await store.get_account(acc.id)
                    if acc2 and acc2.status == "done":
                        await log.emit(f"✓ {acc.label}: готово")
                        return True
                    err_txt = (acc2.last_error if acc2 else "") or "ошибка"
                    await log.emit(f"✗ {acc.label}: {err_txt}", "error")
                    return False

                results = await asyncio.gather(
                    *[_one(a) for a in batch], return_exceptions=True
                )
                for r in results:
                    if r is True:
                        ok += 1
                    else:
                        err += 1
                if i + size < len(accounts):
                    await asyncio.sleep(max(0.0, float(batch_pause)))
            lines.append(f"Аккаунты: ok={ok}, ошибок={err}")

        report = "\n".join(lines) or "Нечего делать"
        await store.finish_job(job.id, "done", report)
        await log.emit("Перенастройка всех завершена.\n" + report)
        return report
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        await store.finish_job(job.id, "error", err)
        await log.emit(f"Ошибка перенастройки всех: {err}", "error")
        return err
