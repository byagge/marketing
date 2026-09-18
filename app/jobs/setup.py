from __future__ import annotations

from pathlib import Path
from typing import Any

from aiogram import Bot
from telethon import TelegramClient

from app.config import get_settings
from app.jobs import LogSink, runtime
from app.models import Account, Chat, Post
from app.sender_api import SenderAPI
from app.store import Store
from app.tg.client import telethon_client
from app.tg.resolve import lookup_entity
from app.tg.scheduler import schedule_chat_posts
from app.tg.unavailable import format_unavailable, is_chat_unavailable
from app.utils.chat_ids import chat_ids_match
from app.utils.entities import entities_loads
from app.utils.minutes import period_for_interval, suggest_minute
from app.utils.templates import render_post


class SetupError(Exception):
    pass


async def ensure_minute(store: Store, chat: Chat, account: Account) -> int:
    existing = await store.slot_for(chat.id, account.id)
    if existing:
        return existing.start_minute
    occupied = await store.occupied_minutes(chat.id)
    minute = suggest_minute(period_for_interval(chat.interval_minutes), occupied)
    await store.set_slot(chat.id, account.id, minute)
    return minute


async def ensure_sender_account(store: Store, account: Account, api: SenderAPI, log: LogSink) -> str:
    if account.sender_account_id:
        info = await api.get_account(account.sender_account_id)
        if not info.get("live"):
            await log.emit(f"Sender {account.label}: поднимаю клиент…")
            await api.start_account(account.sender_account_id)
        return account.sender_account_id

    if not account.pyrogram_session or not account.sender_bot_token:
        raise SetupError(
            "Для sender нужен Pyrogram .session и bot token "
            "(или уже существующий Autoposter account_id)."
        )
    path = Path(account.pyrogram_session)
    if not path.exists():
        raise SetupError("Pyrogram session файл не найден на диске")
    await log.emit(f"Sender {account.label}: создаю аккаунт в Autoposter…")
    created = await api.create_account(
        session_bytes=path.read_bytes(),
        bot_token=account.sender_bot_token,
        label=account.label,
        session_name=path.name,
    )
    sender_id = str(created.get("account_id") or "")
    if not sender_id:
        raise SetupError(f"Autoposter не вернул account_id: {created}")
    await store.update_account(account.id, sender_account_id=sender_id)
    return sender_id


def match_catalog_chat(cid: str | int | None, chats: list[Chat]) -> Chat | None:
    for chat in chats:
        if chat_ids_match(cid, chat.chat_id):
            return chat
    return None


def pick_sender_live_chats(live_chats: list[dict], schedule_chats: list[Chat]) -> list[dict]:
    picked: list[dict] = []
    for live in live_chats:
        cid = str(live.get("chat_id") or "")
        if not cid:
            continue
        if match_catalog_chat(cid, schedule_chats):
            continue
        picked.append(live)
    return picked


async def _mute_schedule_on_sender(
    api: SenderAPI,
    sender_id: str,
    live_chats: list[dict],
    schedule_chats: list[Chat],
) -> None:
    for live in live_chats:
        cid = str(live.get("chat_id") or "")
        if cid and match_catalog_chat(cid, schedule_chats):
            await api.patch_chat(sender_id, cid, active=False)


async def _fail_unavailable(
    store: Store,
    account: Account,
    chat: Chat,
    error: str,
    log: LogSink,
    *,
    max_attempts: int,
) -> str:
    state = await store.record_setup_fail(
        account.id, chat.id, error, max_attempts=max_attempts
    )
    if state.is_abandoned:
        await log.emit(
            f"Пропуск «{chat.title}»: недоступен ({error}). "
            f"Попытка {state.fail_count}/{max_attempts} — больше не пробую "
            f"до начала недели",
            "error",
        )
        return "abandoned"
    await log.emit(
        f"Пропуск «{chat.title}»: недоступен ({error}). "
        f"Попытка {state.fail_count}/{max_attempts}, повтор через "
        f"{get_settings().setup_retry_days} дн.",
        "error",
    )
    return "skipped"


async def schedule_one_chat(
    store: Store,
    client: TelegramClient,
    account: Account,
    chat: Chat,
    posts: dict[str, Post],
    log: LogSink,
    *,
    skip_abandoned: bool = False,
) -> str:
    """
    Schedule posts for one chat.

    Returns: ok | skipped | abandoned | config_error | stopped
    """
    settings = get_settings()
    max_attempts = settings.setup_max_attempts

    if skip_abandoned:
        state = await store.get_setup_state(account.id, chat.id)
        if state and state.is_abandoned:
            await log.emit(
                f"Пропуск «{chat.title}»: уже {state.fail_count} неудачных попыток "
                f"(ждём начало недели)",
                "error",
            )
            return "abandoned"

    post = posts.get(chat.lang) or posts["ru"]
    if not post.text.strip():
        await log.emit(
            f"Пропуск schedule «{chat.title}»: нет текста для языка {chat.lang}",
            "error",
        )
        return "config_error"

    text, entities = render_post(
        post.text,
        entities_loads(post.entities_json),
        chat.tag,
    )
    slot = await store.slot_for(chat.id, account.id)
    minute = slot.start_minute if slot else await ensure_minute(store, chat, account)

    try:
        entity = await lookup_entity(client, chat)
        if entity is None:
            return await _fail_unavailable(
                store,
                account,
                chat,
                "чат не найден в аккаунте",
                log,
                max_attempts=max_attempts,
            )
        result = await schedule_chat_posts(
            client=client,
            target=entity,
            text=text,
            entities=entities,
            start_minute=minute,
            interval_minutes=chat.interval_minutes,
            start_hour=settings.start_hour,
            tz=settings.tz,
            repeat_period=settings.repeat_period or None,
            photo_path=post.photo_path or None,
        )
    except Exception as e:
        if is_chat_unavailable(e):
            return await _fail_unavailable(
                store,
                account,
                chat,
                format_unavailable(e),
                log,
                max_attempts=max_attempts,
            )
        await log.emit(
            f"Ошибка schedule «{chat.title}»: {type(e).__name__}: {e}",
            "error",
        )
        await store.record_setup_fail(
            account.id, chat.id, f"{type(e).__name__}: {e}", max_attempts=max_attempts
        )
        return "skipped"

    if result["success"] < result["planned"]:
        err = result["error"] or f"{result['success']}/{result['planned']}"
        await log.emit(
            f"Частично «{chat.title}»: {result['success']}/{result['planned']}"
            + (f" | {result['error']}" if result["error"] else ""),
            "error",
        )
        await store.record_setup_fail(
            account.id, chat.id, err, max_attempts=max_attempts
        )
        return "skipped"

    await store.record_setup_ok(account.id, chat.id)
    tag_note = f" | тег {chat.tag}" if chat.tag.strip() else ""
    await log.emit(
        f"На аккаунт {account.label} настроил отправку на чат "
        f"«{result['title']}» | :{minute:02d} | "
        f"{result['success']} слотов | старт {result['first_time']}"
        f"{tag_note}"
        + (f" | очистил {result['cleared']} старых" if result["cleared"] else "")
    )
    return "ok"


async def schedule_chats_batch(
    store: Store,
    client: TelegramClient,
    account: Account,
    chats: list[Chat],
    posts: dict[str, Post],
    log: LogSink,
    *,
    skip_abandoned: bool = False,
    job_kind: str = "setup",
) -> dict[str, list[str]]:
    """Schedule many chats; never aborts the whole batch on a missing chat."""
    buckets: dict[str, list[str]] = {
        "ok": [],
        "skipped": [],
        "abandoned": [],
        "config_error": [],
    }
    for chat in chats:
        if runtime.cancelled(job_kind, account.id):
            raise SetupError("Остановлено")
        outcome = await schedule_one_chat(
            store,
            client,
            account,
            chat,
            posts,
            log,
            skip_abandoned=skip_abandoned,
        )
        if outcome == "stopped":
            raise SetupError("Остановлено")
        buckets.setdefault(outcome, []).append(chat.title)
    return buckets


async def run_setup(store: Store, account_id: int, bot: Bot, admin_chat_id: int) -> None:
    settings = get_settings()
    account = await store.get_account(account_id)
    if not account:
        return
    job = await store.create_job("setup", account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    await store.update_account(account.id, status="running", last_error="")

    scheduled_ok: list[str] = []
    scheduled_skip: list[str] = []
    scheduled_abandoned: list[str] = []
    sender_count = 0
    report_lines: list[str] = []

    try:
        posts = {
            "ru": await store.get_post(account.id, "ru"),
            "en": await store.get_post(account.id, "en"),
        }
        if not posts["ru"].text.strip() and not posts["en"].text.strip():
            raise SetupError("Сначала задайте пост этого аккаунта (RU/EN)")

        chats = await store.list_chats(enabled_only=True)
        schedule_chats = [c for c in chats if c.is_schedule]
        sender_chats = [c for c in chats if c.kind == "sender"]

        if not schedule_chats and not account.has_sender:
            raise SetupError("Нет включённых schedule-чатов и sender не готов")
        if schedule_chats and not account.telethon_session:
            raise SetupError("Нет Telethon session")

        await log.emit(
            f"Старт настройки {account.display}\n"
            f"Schedule-чатов: {len(schedule_chats)} | "
            f"Sender: {'все остальные диалоги' if account.has_sender else 'нет'}"
        )

        for chat in schedule_chats:
            await ensure_minute(store, chat, account)

        if schedule_chats:
            async with telethon_client(account.telethon_session) as client:
                me = await client.get_me()
                await log.emit(
                    f"Telethon: {me.first_name} "
                    f"(@{me.username or '-'}) id={me.id}"
                    f"{' | Premium' if getattr(me, 'premium', False) else ''}"
                )
                # Manual setup tries every chat, including previously abandoned.
                buckets = await schedule_chats_batch(
                    store,
                    client,
                    account,
                    schedule_chats,
                    posts,
                    log,
                    skip_abandoned=False,
                    job_kind="setup",
                )
                scheduled_ok = buckets.get("ok", [])
                scheduled_skip = buckets.get("skipped", []) + buckets.get(
                    "config_error", []
                )
                scheduled_abandoned = buckets.get("abandoned", [])

        if account.has_sender:
            api = SenderAPI(settings.sender_api_url, settings.sender_api_key)
            sender_id = await ensure_sender_account(store, account, api, log)
            ss = await store.sender_settings()
            ru_text, _ = render_post(
                posts["ru"].text,
                entities_loads(posts["ru"].entities_json),
                None,
            )
            photo_bytes = None
            if posts["ru"].photo_path and Path(posts["ru"].photo_path).exists():
                photo_bytes = Path(posts["ru"].photo_path).read_bytes()

            await api.put_post(sender_id, ru_text, photo_bytes)
            await log.emit(f"{account.label}: настроил пост RU этого аккаунта")

            await api.put_interval(sender_id, "between", ss.between_min, ss.between_max)
            await api.put_interval(sender_id, "cycle", ss.cycle_min, ss.cycle_max)
            await api.put_interval(sender_id, "per_chat", ss.per_chat_min, ss.per_chat_max)
            await api.put_parallel(sender_id, ss.parallel)
            await log.emit(
                f"{account.label}: настроил интервалы "
                f"{ss.between_min}-{ss.between_max}s | parallel={ss.parallel}"
            )

            await api.put_cloak(sender_id, ss.cloak_enabled, ss.cloak_text)
            await log.emit(
                f"{account.label}: настроил клоакинг "
                f"({'вкл' if ss.cloak_enabled else 'выкл'})"
            )

            live_chats = await api.list_chats(sender_id)
            await _mute_schedule_on_sender(api, sender_id, live_chats, schedule_chats)

            targets = pick_sender_live_chats(live_chats, schedule_chats)
            live_ids = {str(c.get("chat_id") or "") for c in live_chats}
            for chat in sender_chats:
                if not any(chat_ids_match(chat.chat_id, cid) for cid in live_ids):
                    await log.emit(
                        f"Sender: чат каталога «{chat.title}» ({chat.chat_id}) "
                        f"не найден в диалогах аккаунта — пропускаю",
                        "error",
                    )

            for live in targets:
                if runtime.cancelled("setup", account.id):
                    raise SetupError("Остановлено")
                cid = str(live.get("chat_id"))
                catalog = match_catalog_chat(cid, sender_chats)
                post = posts.get(catalog.lang) if catalog else posts["ru"]
                post = post or posts["ru"]
                text, _ = render_post(
                    post.text,
                    entities_loads(post.entities_json),
                    catalog.tag if catalog else None,
                )
                title = (catalog.title if catalog else None) or live.get("title") or cid
                await api.patch_chat(
                    sender_id,
                    cid,
                    active=True,
                    mode="post",
                    text=text,
                )
                sender_count += 1
                await log.emit(f"Sender: включил «{title}»")

            if sender_count:
                await api.spam_start(sender_id)
                await _mute_schedule_on_sender(api, sender_id, live_chats, schedule_chats)
                await log.emit(
                    f"{account.label}: sender запущен на {sender_count} чатов "
                    f"(все кроме schedule)"
                )
            else:
                await log.emit(
                    f"{account.label}: sender — нет чатов кроме schedule, spam/start не вызываю"
                )
        elif sender_chats:
            await log.emit(
                f"{account.label}: в каталоге есть sender-чаты, но sender не готов — пропускаю",
                "error",
            )

        report_lines.append(f"Аккаунт: {account.display}")
        if scheduled_ok:
            report_lines.append(
                "Настроена отправка (schedule) на чаты: " + ", ".join(scheduled_ok)
            )
        elif schedule_chats:
            report_lines.append("Schedule: ни один чат не настроен")
        else:
            report_lines.append("Schedule-чатов не было")
        if scheduled_skip:
            report_lines.append(
                "Пропущены (повтор позже): " + ", ".join(scheduled_skip)
            )
        if scheduled_abandoned:
            report_lines.append(
                "Больше не пробую до начала недели: "
                + ", ".join(scheduled_abandoned)
            )
        report_lines.append(f"Sender настроен на {sender_count} чатов")
        report = "\n".join(report_lines)
        await store.finish_job(job.id, "done", report)
        await store.update_account(account.id, status="done", last_error="")
        await log.emit("Готово.\n" + report)

    except SetupError as e:
        err = str(e)
        if err == "Остановлено":
            await store.finish_job(job.id, "cancelled", err)
            await store.update_account(account.id, status="idle", last_error="")
            await log.emit(f"Настройка {account.label} остановлена")
        else:
            await store.finish_job(job.id, "error", err)
            await store.update_account(account.id, status="error", last_error=err)
            await log.emit(f"Ошибка настройки {account.label}: {err}", "error")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        await store.finish_job(job.id, "error", err)
        await store.update_account(account.id, status="error", last_error=err)
        await log.emit(f"Ошибка настройки {account.label}: {err}", "error")
    finally:
        current = await store.get_account(account_id)
        if current and current.status == "running":
            await store.update_account(account_id, status="idle")


async def run_setup_chats_only(
    store: Store,
    account_id: int,
    chat_pks: list[int],
    bot: Bot | None,
    admin_chat_id: int | None,
    *,
    job_kind: str = "setup_retry",
    skip_abandoned: bool = True,
) -> dict[str, Any]:
    """Schedule only selected chats for an account (used by retry / weekly)."""
    account = await store.get_account(account_id)
    empty = {"ok": [], "skipped": [], "abandoned": [], "config_error": []}
    if not account or not account.telethon_session or not chat_pks:
        return empty

    posts = {
        "ru": await store.get_post(account.id, "ru"),
        "en": await store.get_post(account.id, "en"),
    }
    if not posts["ru"].text.strip() and not posts["en"].text.strip():
        return empty

    chats: list[Chat] = []
    for pk in chat_pks:
        chat = await store.get_chat(pk)
        if chat and chat.enabled and chat.is_schedule:
            chats.append(chat)
    if not chats:
        return empty

    job = await store.create_job(job_kind, account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    try:
        for chat in chats:
            await ensure_minute(store, chat, account)
        async with telethon_client(account.telethon_session) as client:
            buckets = await schedule_chats_batch(
                store,
                client,
                account,
                chats,
                posts,
                log,
                skip_abandoned=skip_abandoned,
                job_kind=job_kind,
            )
        report = (
            f"{account.label}: ok={len(buckets.get('ok', []))} "
            f"skip={len(buckets.get('skipped', []))} "
            f"abandoned={len(buckets.get('abandoned', []))}"
        )
        await store.finish_job(job.id, "done", report)
        return buckets
    except SetupError as e:
        await store.finish_job(job.id, "cancelled" if str(e) == "Остановлено" else "error", str(e))
        return empty
    except Exception as e:
        await store.finish_job(job.id, "error", f"{type(e).__name__}: {e}")
        return empty
