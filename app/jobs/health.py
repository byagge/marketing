from __future__ import annotations

from aiogram import Bot

from app.config import get_settings
from app.jobs import LogSink
from app.store import Store
from app.tg.client import telethon_client
from app.tg.health import check_schedule_chats


def format_health_report(account_label: str, rows: list[dict]) -> str:
    if not rows:
        return f"{account_label}: нет schedule-чатов"
    lines = [f"Проверка schedule | {account_label}"]
    marks = {"ok": "OK", "low": "мало", "empty": "пусто", "error": "ошибка"}
    bad = 0
    for row in rows:
        mark = marks.get(row["status"], row["status"])
        line = f"- {row['title']}: {row['count']}/{row['expected']} ({mark})"
        if row.get("error"):
            line += f" — {row['error']}"
        lines.append(line)
        if row["status"] != "ok":
            bad += 1
    lines.append(f"Проблемных: {bad} из {len(rows)}")
    return "\n".join(lines)


async def run_health(store: Store, account_id: int, bot: Bot, admin_chat_id: int) -> str:
    account = await store.get_account(account_id)
    if not account or not account.telethon_session:
        text = "Нет аккаунта или Telethon session"
        if bot:
            await bot.send_message(admin_chat_id, text)
        return text
    job = await store.create_job("health", account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    chats = await store.list_chats(kind="schedule", enabled_only=True)
    try:
        async with telethon_client(account.telethon_session) as client:
            rows = await check_schedule_chats(client, chats)
        report = format_health_report(account.label, rows)
        await store.finish_job(job.id, "done", report)
        await log.emit(report)
        return report
    except Exception as e:
        err = f"{account.label}: проверка не удалась — {e}"
        await store.finish_job(job.id, "error", err)
        await log.emit(err, "error")
        return err


async def run_health_all(store: Store, bot: Bot, admin_chat_id: int) -> None:
    accounts = await store.list_accounts()
    if not accounts:
        await bot.send_message(admin_chat_id, "Нет аккаунтов для проверки schedule")
        return
    await bot.send_message(admin_chat_id, f"Недельная проверка schedule | {len(accounts)} аккаунтов")
    for acc in accounts:
        if not acc.telethon_session:
            continue
        try:
            await run_health(store, acc.id, bot, admin_chat_id)
        except Exception:
            continue


def weekly_cron_args():
    settings = get_settings()
    return {
        "day_of_week": settings.weekly_health_dow,
        "hour": settings.weekly_health_hour,
        "minute": 0,
        "timezone": settings.timezone,
    }
