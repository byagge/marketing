"""Пересборка минутной таблицы + массовая перенастройка аккаунтов."""

from __future__ import annotations

from aiogram import Bot

from app.jobs import LogSink, runtime
from app.store import Store
from app.utils.minutes import period_for_interval, plan_chat_minutes


async def rebalance_minute_table(store: Store) -> dict[str, int]:
    """
    Полностью пересобрать слоты schedule-чатов:
    внутри чата — равномерно, между чатами — со сдвигом фазы.
    """
    from app.utils.minutes import suggest_minute

    chats = [c for c in await store.list_chats(kind="schedule") if c.enabled]
    accounts = await store.list_accounts()
    if not chats or not accounts:
        return {"chats": len(chats), "accounts": len(accounts), "slots": 0}

    for chat in chats:
        for slot in await store.slots_for_chat(chat.id):
            await store.delete_slot(chat.id, slot.account_id)

    slots_written = 0
    total = len(chats)
    for idx, chat in enumerate(chats):
        period = period_for_interval(chat.interval_minutes)
        minutes = plan_chat_minutes(
            period,
            min(len(accounts), period),
            chat_index=idx,
            chats_total=total,
        )
        occupied: list[int] = list(minutes)
        assigned = list(zip(accounts[: len(minutes)], minutes))
        # если аккаунтов больше периода — добираем оставшиеся свободные минуты
        for account in accounts[len(minutes) :]:
            try:
                m = suggest_minute(period, occupied)
            except Exception:
                break
            occupied.append(m)
            assigned.append((account, m))
        for account, minute in assigned:
            await store.set_slot(chat.id, account.id, minute)
            slots_written += 1
    return {"chats": len(chats), "accounts": len(accounts), "slots": slots_written}


async def run_reconfigure_all(
    store: Store,
    bot: Bot,
    admin_chat_id: int,
    *,
    rebalance: bool = True,
    setup_accounts: bool = True,
) -> str:
    """
    1) Пересобрать таблицу минут
    2) По очереди перенастроить каждый аккаунт (schedule + sender + cloak)
    """
    from app.jobs.setup import run_setup

    job = await store.create_job("reconfigure_all", None)
    log = LogSink(store, job.id, bot, admin_chat_id)
    lines: list[str] = []

    try:
        if rebalance:
            await log.emit("Пересобираю минутную таблицу равномерно…")
            stats = await rebalance_minute_table(store)
            msg = (
                f"Таблица: {stats['slots']} слотов "
                f"({stats['accounts']} акк. × {stats['chats']} чатов)"
            )
            lines.append(msg)
            await log.emit(msg)

        if setup_accounts:
            accounts = await store.list_accounts()
            await log.emit(f"Перенастраиваю аккаунты по одному: {len(accounts)}")
            ok = err = 0
            for acc in accounts:
                if runtime.cancelled("reconfigure_all", 0):
                    raise RuntimeError("Остановлено")
                if runtime.is_running("setup", acc.id):
                    await log.emit(f"Пропуск {acc.label}: уже идёт setup", "error")
                    err += 1
                    continue
                await log.emit(f"→ {acc.label}: старт настройки")
                try:
                    done = runtime.spawn(
                        "setup",
                        acc.id,
                        run_setup(store, acc.id, bot, admin_chat_id),
                    )
                    await done
                except Exception as e:
                    err += 1
                    await log.emit(
                        f"✗ {acc.label}: задача упала ({type(e).__name__}: {e})",
                        "error",
                    )
                    continue
                acc2 = await store.get_account(acc.id)
                if acc2 and acc2.status == "done":
                    ok += 1
                    await log.emit(f"✓ {acc.label}: готово")
                else:
                    err += 1
                    err_txt = (acc2.last_error if acc2 else "") or "ошибка"
                    await log.emit(f"✗ {acc.label}: {err_txt}", "error")
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
