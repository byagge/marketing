"""Проверка членства и автовступление в чаты каталога."""

from __future__ import annotations

from aiogram import Bot

from app.jobs import LogSink, runtime
from app.models import Account, Chat
from app.store import Store
from app.tg.client import telethon_client
from app.tg.join import JoinResult, check_membership, join_many


async def _enabled_chats(store: Store) -> list[Chat]:
    return [c for c in await store.list_chats(enabled_only=True)]


async def run_membership_check(
    store: Store,
    account_id: int,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
) -> dict:
    account = await store.get_account(account_id)
    if not account or not account.telethon_session:
        return {
            "ok": False,
            "error": "Нужен Telethon session",
            "joined": [],
            "missing": [],
            "no_link": [],
        }
    chats = await _enabled_chats(store)
    job = await store.create_job("membership_check", account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    try:
        await log.emit(f"{account.label}: проверка вступления в {len(chats)} чатов…")
        async with telethon_client(account.telethon_session) as client:
            report = await check_membership(client, chats)
        payload = {
            "ok": True,
            "joined": report.joined,
            "missing": report.missing,
            "no_link": report.no_link,
            "joined_n": len(report.joined),
            "missing_n": len(report.missing),
            "no_link_n": len(report.no_link),
        }
        summary = (
            f"{account.label}: в чатах {payload['joined_n']}, "
            f"не вступлен {payload['missing_n']}, "
            f"без ссылки {payload['no_link_n']}"
        )
        await store.finish_job(job.id, "done", summary)
        await log.emit(summary)
        return payload
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        await store.finish_job(job.id, "error", err)
        await log.emit(err, "error")
        return {
            "ok": False,
            "error": err,
            "joined": [],
            "missing": [],
            "no_link": [],
        }


async def run_join_chats(
    store: Store,
    account_id: int,
    chat_pks: list[int] | None,
    bot: Bot,
    admin_chat_id: int,
    *,
    job_kind: str = "join_chats",
) -> list[JoinResult]:
    account = await store.get_account(account_id)
    if not account or not account.telethon_session:
        raise RuntimeError("Нужен Telethon session")

    if chat_pks is None:
        chats = await _enabled_chats(store)
    else:
        chats = []
        for pk in chat_pks:
            chat = await store.get_chat(pk)
            if chat and chat.enabled:
                chats.append(chat)

    job = await store.create_job(job_kind, account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    results: list[JoinResult] = []
    try:
        await log.emit(
            f"{account.label}: вступаю в {len(chats)} чатов "
            f"(invite / @username / капча)…"
        )

        async def progress(i: int, total: int, result: JoinResult) -> None:
            if runtime.cancelled(job_kind, account.id):
                raise RuntimeError("Остановлено")
            mark = {
                "joined": "✓",
                "already": "·",
                "captcha_ok": "✓+",
                "request_sent": "📩",
                "needs_manual": "✋",
                "failed": "✗",
                "skipped": "…",
            }.get(result.status, "?")
            await log.emit(
                f"{mark} [{i}/{total}] {result.chat_title}: "
                f"{result.status}"
                + (f" — {result.detail}" if result.detail else "")
            )

        async with telethon_client(account.telethon_session) as client:
            results = await join_many(client, chats, progress=progress)

        ok = sum(
            1
            for r in results
            if r.status in {"joined", "already", "captcha_ok", "request_sent"}
        )
        manual = sum(1 for r in results if r.status == "needs_manual")
        fail = sum(1 for r in results if r.status == "failed")
        report = f"{account.label}: ok={ok}, вручную={manual}, ошибок={fail}"
        await store.finish_job(job.id, "done", report)
        await log.emit("Готово. " + report)
        return results
    except Exception as e:
        err = str(e)
        status = "cancelled" if err == "Остановлено" else "error"
        await store.finish_job(job.id, status, err)
        await log.emit(f"Вступление прервано: {err}", "error")
        return results


async def run_folder_join(
    store: Store,
    account_id: int,
    folder_link: str,
    bot: Bot,
    admin_chat_id: int,
) -> dict:
    from app.tg.join import _maybe_solve_captcha
    from app.tg.join_flows import join_folder, parse_folder_slug

    account = await store.get_account(account_id)
    if not account or not account.telethon_session:
        raise RuntimeError("Нужен Telethon session")
    if not parse_folder_slug(folder_link) and "addlist" not in folder_link:
        # всё равно попробуем как slug
        pass

    job = await store.create_job("folder_join", account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    try:
        await log.emit(f"{account.label}: вступление в папку {folder_link}")

        async def solve_fn(client, entity):
            return await _maybe_solve_captcha(client, entity, captcha_kind="auto")

        async with telethon_client(account.telethon_session) as client:
            result = await join_folder(client, folder_link, solve_captcha_fn=solve_fn)

        if result.get("ok"):
            caps = result.get("captchas") or []
            report = (
                f"папка ok, чатов≈{result.get('joined', 0)}, "
                f"капч={len(caps)}"
            )
            if caps:
                report += "\n" + "\n".join(caps[:10])
            await store.finish_job(job.id, "done", report)
            await log.emit("Готово. " + report)
        else:
            err = result.get("error") or "ошибка"
            await store.finish_job(job.id, "error", err)
            await log.emit(f"Папка: {err}", "error")
        return result
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        await store.finish_job(job.id, "error", err)
        await log.emit(err, "error")
        return {"ok": False, "error": err}


async def run_join_all_accounts(
    store: Store,
    bot: Bot,
    admin_chat_id: int,
) -> str:
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    job = await store.create_job("join_all_accounts", None)
    log = LogSink(store, job.id, bot, admin_chat_id)
    lines: list[str] = []
    try:
        await log.emit(f"Вступление во все чаты на {len(accounts)} аккаунтах…")
        for acc in accounts:
            if runtime.cancelled("join_all_accounts", 0):
                raise RuntimeError("Остановлено")
            try:
                results = await run_join_chats(
                    store, acc.id, None, bot, admin_chat_id, job_kind="join_chats"
                )
                ok = sum(
                    1 for r in results if r.status in {"joined", "already", "captcha_ok"}
                )
                lines.append(f"{acc.label}: ok={ok}/{len(results)}")
            except Exception as e:
                lines.append(f"{acc.label}: {type(e).__name__}: {e}")
                await log.emit(f"✗ {acc.label}: {e}", "error")
        report = "\n".join(lines)
        await store.finish_job(job.id, "done", report)
        await log.emit("Массовое вступление завершено.\n" + report)
        return report
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        await store.finish_job(job.id, "error", err)
        await log.emit(err, "error")
        return err
