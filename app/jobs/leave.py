from __future__ import annotations

from aiogram import Bot

from app.jobs import LogSink, parse_keep_ids
from app.store import Store
from app.tg.client import telethon_client
from app.tg.leave import execute_leave, format_dialog, plan_leave


async def preview_leave(store: Store, account_id: int) -> tuple[list[str], list[str]]:
    account = await store.get_account(account_id)
    if not account or not account.telethon_session:
        raise RuntimeError("Нет Telethon session")
    chats = await store.list_chats(enabled_only=True)
    ss = await store.sender_settings()
    keep = parse_keep_ids(ss.keep_extra_ids, [c.chat_id for c in chats])
    async with telethon_client(account.telethon_session) as client:
        kept, to_leave = await plan_leave(client, keep)
    return [format_dialog(d) for d in kept], [format_dialog(d) for d in to_leave]


async def run_leave(store: Store, account_id: int, bot: Bot, admin_chat_id: int) -> None:
    account = await store.get_account(account_id)
    if not account or not account.telethon_session:
        raise RuntimeError("Нет Telethon session")
    job = await store.create_job("leave", account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    chats = await store.list_chats(enabled_only=True)
    ss = await store.sender_settings()
    keep = parse_keep_ids(ss.keep_extra_ids, [c.chat_id for c in chats])
    await log.emit(
        f"{account.label}: выхожу из всех чатов, кроме каталога "
        f"({len(chats)} шт.) и Saved Messages"
    )
    try:
        async with telethon_client(account.telethon_session) as client:
            async def progress(i, total, name):
                if i == 1 or i == total or i % 15 == 0:
                    await log.emit(f"{account.label}: leave {i}/{total} | {name}", notify=False)

            result = await execute_leave(client, keep, progress=progress)
        report = (
            f"{account.label}: вышел из {result['left_ok']}/{result['total']}, "
            f"ошибок {result['left_fail']}. Осталось {len(result['kept'])}."
        )
        await store.finish_job(job.id, "done", report)
        await log.emit(report)
        if result["errors"]:
            await log.emit("Ошибки:\n" + "\n".join(result["errors"]), "error")
    except Exception as e:
        await store.finish_job(job.id, "error", str(e))
        await log.emit(f"{account.label}: leave ошибка — {e}", "error")
        raise
