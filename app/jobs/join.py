"""Проверка членства и автовступление в чаты каталога."""

from __future__ import annotations

from aiogram import Bot

from app.jobs import LogSink, runtime
from app.models import Account, Chat
from app.store import Store
from app.tg.client import telethon_client
from app.tg.join import JoinResult, check_membership, join_many
from app.tg.resolve import lookup_entity
from app.tg.restrictions import classify_text, record_restriction
from app.utils.chat_ids import canon_chat_id
from app.utils.errfmt import short_error


async def _enabled_chats(store: Store) -> list[Chat]:
    return [c for c in await store.list_chats(enabled_only=True)]


async def _joinable_chats(
    store: Store, account_id: int, chats: list[Chat]
) -> tuple[list[Chat], list[Chat], list[Chat]]:
    """(можно вступать, забанен, выключен вручную) — в забаненные не вступаем."""
    banned = await store.banned_pairs()
    disabled = await store.disabled_pairs()
    ok: list[Chat] = []
    banned_chats: list[Chat] = []
    off: list[Chat] = []
    for chat in chats:
        if (account_id, chat.id) in banned:
            banned_chats.append(chat)
        elif (account_id, canon_chat_id(chat.chat_id)) in disabled:
            off.append(chat)
        else:
            ok.append(chat)
    return ok, banned_chats, off


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
    all_chats = await _enabled_chats(store)
    chats, banned_chats, off_chats = await _joinable_chats(store, account.id, all_chats)
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
            "banned": banned_chats,
            "disabled": off_chats,
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
        err = f"{short_error(e)}"
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
    notify_each: bool = True,
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
    # в забаненные чаты не пытаемся вступать; выключенные вручную — тоже
    chats, banned_chats, off_chats = await _joinable_chats(store, account.id, chats)

    job = await store.create_job(job_kind, account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    results: list[JoinResult] = []
    try:
        await log.emit(
            f"{account.label}: вступаю в {len(chats)} чатов "
            f"(invite / @username / капча)…"
            + (f" Пропущено: бан {len(banned_chats)}" if banned_chats else "")
            + (f", выкл. {len(off_chats)}" if off_chats else ""),
            notify=notify_each,
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
                + (f" — {result.detail}" if result.detail else ""),
                notify=notify_each,
            )

        async with telethon_client(account.telethon_session) as client:
            results = await join_many(client, chats, progress=progress)
            chat_by_pk = {c.id: c for c in chats}
            for r in results:
                # бан при вступлении → в базу банов, больше не пробуем
                if r.status == "failed" and classify_text(r.detail) == "ban":
                    chat = chat_by_pk.get(r.chat_pk)
                    if chat is None:
                        continue
                    try:
                        entity = await lookup_entity(client, chat)
                    except Exception:  # noqa: BLE001
                        entity = None
                    await record_restriction(
                        store, client, account, chat, entity, hint="ban", error=r.detail
                    )

        ok = sum(
            1
            for r in results
            if r.status in {"joined", "already", "captcha_ok", "request_sent"}
        )
        manual = sum(1 for r in results if r.status == "needs_manual")
        fail = sum(1 for r in results if r.status == "failed")
        report = f"{account.label}: ok={ok}, вручную={manual}, ошибок={fail}"
        await store.finish_job(job.id, "done", report)
        await log.emit("Готово. " + report, notify=notify_each)
        await _schedule_after_join(store, account, results, chats, bot, admin_chat_id)
        return results
    except Exception as e:
        err = str(e)
        status = "cancelled" if err == "Остановлено" else "error"
        await store.finish_job(job.id, status, err)
        await log.emit(f"Вступление прервано: {err}", "error")
        return results


async def _schedule_after_join(
    store: Store,
    account: Account,
    results: list[JoinResult],
    chats: list[Chat],
    bot: Bot | None,
    admin_chat_id: int | None,
) -> None:
    """Вступили → пара сразу снова доступна: сбрасываем «abandoned» и настраиваем schedule.

    Раньше после вступления счётчик неудач оставался (3/3) до недельного сброса
    в понедельник, и чат (например CARTEL) молчал.
    """
    from app.jobs.setup import run_setup_chats_only

    by_pk = {c.id: c for c in chats}
    pks: list[int] = []
    for r in results:
        if r.status not in {"joined", "already", "captcha_ok"}:
            continue
        chat = by_pk.get(r.chat_pk)
        if chat is None or not chat.is_schedule:
            continue
        state = await store.get_setup_state(account.id, chat.id)
        if state is not None and state.is_ok:
            continue
        await store.reset_setup_state(account.id, chat.id)
        await store.resolve_restriction(account.id, chat.id)
        pks.append(chat.id)
    if not pks:
        return
    try:
        await run_setup_chats_only(
            store,
            account.id,
            pks,
            bot,
            admin_chat_id,
            job_kind="setup_after_join",
            skip_abandoned=False,
        )
    except Exception:  # noqa: BLE001
        pass


async def run_join_missing_all(
    store: Store,
    bot: Bot,
    admin_chat_id: int,
) -> str:
    """Во всех аккаунтах вступить во все недостающие чаты (кроме банов и выключенных)."""
    from app.jobs.parallel import map_batches, setup_parallel_defaults

    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    job = await store.create_job("join_missing_all", None)
    log = LogSink(store, job.id, bot, admin_chat_id)
    batch_size, batch_pause = setup_parallel_defaults()
    no_link_titles: set[str] = set()
    try:
        await log.emit(
            f"Вступление в недостающие чаты: {len(accounts)} акк., "
            f"параллельно ×{batch_size} (баны и выключенные пропускаю)…"
        )

        async def _one(acc: Account) -> str:
            if runtime.cancelled("join_missing_all", 0):
                return f"{acc.label}: остановлено"
            report = await run_membership_check(store, acc.id)
            if not report.get("ok"):
                return f"{acc.label}: ошибка проверки — {report.get('error')}"
            for chat in report.get("no_link") or []:
                no_link_titles.add(chat.display_name)
            missing = [c.id for c in report.get("missing") or []]
            skipped = len(report.get("banned") or [])
            if not missing:
                return f"{acc.label}: недостающих нет" + (
                    f" (банов пропущено {skipped})" if skipped else ""
                )
            results = await run_join_chats(
                store,
                acc.id,
                missing,
                bot,
                admin_chat_id,
                job_kind="join_chats",
                notify_each=False,
            )
            ok = sum(1 for r in results if r.status in {"joined", "already", "captcha_ok"})
            req = sum(1 for r in results if r.status == "request_sent")
            manual = sum(1 for r in results if r.status == "needs_manual")
            fail = [r.chat_title for r in results if r.status == "failed"]
            line = f"{acc.label}: вступил {ok}/{len(missing)}"
            if req:
                line += f", заявок {req}"
            if manual:
                line += f", вручную {manual}"
            if fail:
                line += f", не удалось: {', '.join(fail[:4])}"
            return line

        results = await map_batches(
            accounts,
            _one,
            batch_size=batch_size,
            batch_pause=batch_pause,
            is_cancelled=lambda: runtime.cancelled("join_missing_all", 0),
        )
        lines = [str(r) if not isinstance(r, BaseException) else f"error: {r}" for r in results]
        if no_link_titles:
            lines.append(
                "Нет ссылки/способа вступления (добавьте invite в карточке чата): "
                + ", ".join(sorted(no_link_titles))
            )
        report = "\n".join(lines)
        await store.finish_job(job.id, "done", report)
        await log.emit("Вступление в недостающие завершено.\n" + report)
        return report
    except Exception as e:  # noqa: BLE001
        err = f"{short_error(e)}"
        await store.finish_job(job.id, "error", err)
        await log.emit(err, "error")
        return err


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
        err = f"{short_error(e)}"
        await store.finish_job(job.id, "error", err)
        await log.emit(err, "error")
        return {"ok": False, "error": err}


async def run_join_all_accounts(
    store: Store,
    bot: Bot,
    admin_chat_id: int,
) -> str:
    from app.jobs.parallel import map_batches, setup_parallel_defaults

    accounts = [
        a
        for a in await store.list_accounts()
        if a.telethon_session and not a.is_dead
    ]
    job = await store.create_job("join_all_accounts", None)
    log = LogSink(store, job.id, bot, admin_chat_id)
    lines: list[str] = []
    batch_size, batch_pause = setup_parallel_defaults()
    try:
        await log.emit(
            f"Вступление во все чаты: {len(accounts)} акк., параллельно ×{batch_size}…"
        )

        async def _one(acc: Account) -> str:
            if runtime.cancelled("join_all_accounts", 0):
                return f"{acc.label}: остановлено"
            try:
                results = await run_join_chats(
                    store, acc.id, None, bot, admin_chat_id, job_kind="join_chats"
                )
                ok = sum(
                    1 for r in results if r.status in {"joined", "already", "captcha_ok"}
                )
                line = f"{acc.label}: ok={ok}/{len(results)}"
                await log.emit(f"✓ {line}", notify=False)
                return line
            except Exception as e:
                line = f"{acc.label}: {short_error(e)}"
                await log.emit(f"✗ {line}", "error")
                return line

        async def _on_batch(n: int, batch):
            await log.emit(f"Пачка {n}: " + ", ".join(a.label for a in batch))

        results = await map_batches(
            accounts,
            _one,
            batch_size=batch_size,
            batch_pause=batch_pause,
            is_cancelled=lambda: runtime.cancelled("join_all_accounts", 0),
            on_batch=_on_batch,
        )
        for r in results:
            if isinstance(r, BaseException):
                lines.append(f"error: {r}")
            else:
                lines.append(str(r))
        report = "\n".join(lines)
        await store.finish_job(job.id, "done", report)
        await log.emit("Массовое вступление завершено.\n" + report)
        return report
    except Exception as e:
        err = f"{short_error(e)}"
        await store.finish_job(job.id, "error", err)
        await log.emit(err, "error")
        return err
