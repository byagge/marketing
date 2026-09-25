"""Пересборка минутной таблицы + массовая перенастройка аккаунтов."""

from __future__ import annotations

from collections import defaultdict

from aiogram import Bot

from app.jobs import LogSink, runtime
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.store import Store
from app.utils.minutes import TABLE_PERIOD, plan_chat_minutes, suggest_minute


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


async def run_fix_assigned_minutes(
    store: Store,
    bot: Bot,
    admin_chat_id: int,
    *,
    batch_size: int | None = None,
    batch_pause: float | None = None,
) -> str:
    """
    1) Выровнять минуты у уже назначенных слотов
    2) Переназначить schedule пачками параллельно
    """
    from app.jobs.setup import run_setup_chats_only

    default_size, default_pause = setup_parallel_defaults()
    batch_size = int(batch_size if batch_size is not None else default_size)
    batch_pause = float(batch_pause if batch_pause is not None else default_pause)

    job = await store.create_job("fix_minutes", None)
    log = LogSink(store, job.id, bot, admin_chat_id)
    lines: list[str] = []

    try:
        await log.emit("Выравниваю минуты назначенных слотов по часу…")
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
            f"параллельно ×{batch_size} (пауза {batch_pause:.1f}с)…"
        )

        async def _one(aid: int) -> bool:
            if runtime.cancelled("fix_minutes", 0):
                return False
            pks = chat_pks_by_account.get(aid) or []
            if not pks:
                return True
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
                acc = await store.get_account(aid)
                label = acc.label if acc else f"acc#{aid}"
                await log.emit(f"✓ {label}: schedule ok={n_ok}", notify=False)
                return True
            except Exception as e:
                acc = await store.get_account(aid)
                label = acc.label if acc else f"acc#{aid}"
                await log.emit(f"✗ {label}: {type(e).__name__}: {e}", "error")
                return False

        async def _on_batch(n: int, batch):
            labels = []
            for a in batch:
                acc = await store.get_account(a)
                labels.append(acc.label if acc else str(a))
            await log.emit(f"Пачка {n}: {', '.join(labels)}")

        results = await map_batches(
            account_ids,
            _one,
            batch_size=batch_size,
            batch_pause=batch_pause,
            is_cancelled=lambda: runtime.cancelled("fix_minutes", 0),
            on_batch=_on_batch,
        )
        ok_total = sum(1 for r in results if r is True)
        err_total = len(results) - ok_total
        stopped = runtime.cancelled("fix_minutes", 0)
        lines.append(f"Schedule: ok={ok_total}, ошибок={err_total}")
        if stopped:
            lines.append("Остановлено пользователем")
        report = "\n".join(lines)
        status = "cancelled" if stopped else "done"
        await store.finish_job(job.id, status, report)
        await log.emit(
            ("Выравнивание остановлено.\n" if stopped else "Выравнивание минут завершено.\n")
            + report
        )
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
    batch_size: int | None = None,
    batch_pause: float | None = None,
) -> str:
    """
    1) Пересобрать таблицу минут равномерно
    2) Перенастроить аккаунты пачками параллельно
    """
    from app.jobs.setup import run_setup

    default_size, default_pause = setup_parallel_defaults()
    batch_size = int(batch_size if batch_size is not None else default_size)
    batch_pause = float(batch_pause if batch_pause is not None else default_pause)

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
                f"Перенастраиваю {len(accounts)} акк. параллельно ×{batch_size}…"
            )

            async def _one(acc):
                if runtime.cancelled("reconfigure_all", 0):
                    return False
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

            async def _on_batch(n: int, batch):
                await log.emit(
                    f"Пачка {n}: " + ", ".join(a.label for a in batch)
                )

            results = await map_batches(
                accounts,
                _one,
                batch_size=batch_size,
                batch_pause=batch_pause,
                is_cancelled=lambda: runtime.cancelled("reconfigure_all", 0),
                on_batch=_on_batch,
            )
            ok = sum(1 for r in results if r is True)
            err = len(results) - ok
            lines.append(f"Аккаунты: ok={ok}, ошибок={err}")
            if runtime.cancelled("reconfigure_all", 0):
                lines.append("Остановлено пользователем")

        report = "\n".join(lines) or "Нечего делать"
        stopped = runtime.cancelled("reconfigure_all", 0)
        await store.finish_job(job.id, "cancelled" if stopped else "done", report)
        await log.emit(
            ("Перенастройка остановлена.\n" if stopped else "Перенастройка всех завершена.\n")
            + report
        )
        return report
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        await store.finish_job(job.id, "error", err)
        await log.emit(f"Ошибка перенастройки всех: {err}", "error")
        return err
