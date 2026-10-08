from __future__ import annotations

from pathlib import Path
from typing import Any

from aiogram import Bot
from telethon import TelegramClient

from app.config import get_settings
from app.jobs import LogSink, runtime
from app.models import Account, Chat, Post
from app.sender_api import SenderAPI, SenderAPIError
from app.store import Store
from app.tg.client import telethon_client
from app.tg.resolve import lookup_entity
from app.tg.scheduler import (
    is_media_forbidden_error,
    peer_allows_photos,
    schedule_chat_forwards,
    schedule_chat_posts,
)
from app.jobs.bans import register_ban
from app.tg.unavailable import format_unavailable, is_chat_unavailable, is_unavailable_text
from app.utils.chat_ids import chat_ids_match
from app.utils.entities import entities_loads
from app.utils.minutes import period_for_interval, suggest_minute
from app.jobs.spam import account_load
from app.utils.send_policy import (
    classify_failure,
    effective_interval_minutes,
    effective_interval_seconds,
    is_send_allowed,
    matches_stoplist,
    pick_text_override,
)
from app.utils.spamstate import scale_parallel, scale_seconds
from app.utils.schedule import resolve_repeat_period
from app.utils.templates import pick_post_for_chat, render_post
from app.tg.sender_push import (
    apply_mentions_everywhere,
    has_premium_emoji,
    normalize_multiline,
    push_chat_text,
    push_cloak,
    push_sender_post,
)


class SetupError(Exception):
    pass


async def ensure_minute(store: Store, chat: Chat, account: Account) -> int:
    existing = await store.slot_for(chat.id, account.id)
    if existing:
        return existing.start_minute

    period = period_for_interval(chat.interval_minutes)
    occupied = await store.occupied_minutes(chat.id)
    all_slots = await store.all_slots()
    schedule_ids = {
        c.id for c in await store.list_chats(kind="schedule") if c.enabled
    }
    global_counts: dict[int, int] = {}
    for slot in all_slots:
        if slot.chat_pk not in schedule_ids:
            continue
        global_counts[slot.start_minute] = global_counts.get(slot.start_minute, 0) + 1

    minute = suggest_minute(period, occupied, global_counts)
    await store.set_slot(chat.id, account.id, minute)
    return minute


async def ensure_sender_account(store: Store, account: Account, api: SenderAPI, log: LogSink) -> str:
    sender_id = (account.sender_account_id or "").strip()
    if sender_id:
        await log.emit(
            f"Sender {account.label}: использую существующий ID "
            f"{sender_id} (без повторной загрузки session)"
        )
        info = await api.get_account(sender_id)
        if not info.get("live"):
            await log.emit(f"Sender {account.label}: поднимаю клиент…")
            await api.start_account(sender_id)
        if sender_id != (account.sender_account_id or ""):
            await store.update_account(account.id, sender_account_id=sender_id)
        return sender_id

    if not account.pyrogram_session or not (account.sender_bot_token or "").strip():
        raise SetupError(
            "Для sender укажите Sender ID (если аккаунт уже в Autoposter) "
            "либо Pyrogram .session + bot token для создания нового."
        )
    path = Path(account.pyrogram_session)
    if not path.exists():
        raise SetupError("Pyrogram session файл не найден на диске")
    await log.emit(f"Sender {account.label}: создаю аккаунт в Autoposter…")
    created = await api.create_account(
        session_bytes=path.read_bytes(),
        bot_token=account.sender_bot_token.strip(),
        label=account.label,
        session_name=path.name,
    )
    created_id = str(created.get("account_id") or "").strip()
    if not created_id:
        raise SetupError(f"Autoposter не вернул account_id: {created}")
    await store.update_account(account.id, sender_account_id=created_id)
    await log.emit(f"Sender {account.label}: создан, ID={created_id}")
    return created_id


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
            # schedule не в рассылке + без отметок
            # schedule не в рассылке; mention по общей настройке не трогаем здесь
            await api.patch_chat(sender_id, cid, active=False, mention=False)


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
    if classify_failure(error) == "ban":
        await register_ban(
            store, account, chat, "send_ban", error, log.bot, log.chat_id
        )
    prefix = f"{account.label}: "
    if state.is_abandoned:
        await log.emit(
            f"{prefix}Пропуск «{chat.title}»: недоступен ({error}). "
            f"Попытка {state.fail_count}/{max_attempts} — больше не пробую "
            f"до начала недели",
            "error",
        )
        return "abandoned"
    await log.emit(
        f"{prefix}Пропуск «{chat.title}»: недоступен ({error}). "
        f"Попытка {state.fail_count}/{max_attempts}, повтор через "
        f"{get_settings().setup_retry_days} дн.",
        "error",
    )
    return "skipped"


async def clear_scheduled_for_pair(
    client: TelegramClient,
    chat: Chat,
    account: Account,
    reason: str,
    log: LogSink,
) -> int:
    """Пара выключена/забанена — убираем уже запланированные сообщения в чате."""
    from app.tg.scheduler import delete_scheduled

    try:
        entity = await lookup_entity(client, chat)
        if entity is None:
            return 0
        deleted = await delete_scheduled(
            client, entity, pause=get_settings().schedule_delete_pause_sec
        )
    except Exception as e:
        await log.emit(
            f"{account.label}: «{chat.title}» отключён ({reason}), но очистка "
            f"schedule не удалась: {type(e).__name__}: {e}",
            "error",
            notify=False,
        )
        return 0
    await log.emit(
        f"{account.label}: «{chat.title}» отключён ({reason}) — убрал {deleted} "
        f"запланированных",
        notify=False,
    )
    return deleted


async def schedule_one_chat(
    store: Store,
    client: TelegramClient,
    account: Account,
    chat: Chat,
    posts: dict[str, Post],
    log: LogSink,
    *,
    skip_abandoned: bool = False,
    is_premium: bool = False,
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
                f"{account.label}: Пропуск «{chat.title}»: уже {state.fail_count} "
                f"неудачных попыток (ждём начало недели)",
                "error",
            )
            return "abandoned"

    pref = await store.get_chat_pref(account.id, chat.id)
    ban = await store.get_ban(account.id, chat.id)
    allowed, why = is_send_allowed(
        account,
        chat,
        pref,
        bool(ban and ban.active),
        stoplist=await store.get_stoplist(),
    )
    if not allowed:
        await clear_scheduled_for_pair(client, chat, account, why, log)
        return "disabled"
    override = pick_text_override(pref)
    interval_min = effective_interval_minutes(chat)

    post = pick_post_for_chat(posts, chat)
    use_link = (
        override is None
        and post.is_link_mode
        and (post.has_text_link or post.has_photo_link)
    )
    if override is None and not use_link and not post.text.strip():
        await log.emit(
            f"{account.label}: Пропуск schedule «{chat.title}»: нет текста "
            f"({'короткий ' if chat.uses_short_text else ''}{chat.lang})",
            "error",
        )
        return "config_error"
    if use_link:
        want_photo = chat.media_allowed
        fwd = post.pick_forward(want_photo=want_photo)
        if not fwd:
            await log.emit(
                f"{account.label}: Пропуск schedule «{chat.title}»: нет ссылки "
                f"({'фото' if want_photo else 'текст'})",
                "error",
            )
            return "config_error"
    elif override is not None:
        # свой текст пары — строго как задан, без фото аккаунта
        text, entities = render_post(
            override,
            entities_loads(pref.entities_json),
            chat.tag,
        )
    else:
        text, entities = render_post(
            post.text,
            entities_loads(post.entities_json),
            chat.tag,
        )
    photo_src = None if override is not None else (post.photo_path or None)
    slot = await store.slot_for(chat.id, account.id)
    minute = slot.start_minute if slot else await ensure_minute(store, chat, account)
    repeat_period = resolve_repeat_period(is_premium, settings.repeat_period)

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
        # заранее: если в чат нельзя фото — сразу без медиа (caption/текст)
        want_media = chat.media_allowed
        if want_media and (
            (use_link and post.has_photo_link)
            or (not use_link and bool(photo_src))
        ):
            if not await peer_allows_photos(client, entity):
                want_media = False
                await store.update_chat(chat.id, allow_media=0)
                chat = await store.get_chat(chat.id) or chat
                await log.emit(
                    f"{account.label}: «{chat.title}»: фото запрещено правами — "
                    f"выкладываю только текст"
                )
        if use_link:
            from_peer, msg_id = post.pick_forward(want_photo=want_media)  # type: ignore[misc]
            assert from_peer is not None
            result = await schedule_chat_forwards(
                client=client,
                target=entity,
                from_peer=from_peer,
                message_id=msg_id,
                start_minute=minute,
                interval_minutes=interval_min,
                start_hour=settings.start_hour,
                tz=settings.tz,
                repeat_period=repeat_period,
            )
            # если фото-ссылка не прошла из-за медиа — пробуем text-link
            if (
                result["success"] < result["planned"]
                and want_media
                and post.has_photo_link
                and post.has_text_link
            ):
                err = result.get("error") or ""
                if (
                    is_media_forbidden_error(RuntimeError(err))
                    or "media" in err.casefold()
                    or "photo" in err.casefold()
                    or "forbidden" in err.casefold()
                    or result["success"] == 0
                ):
                    await store.update_chat(chat.id, allow_media=0)
                    chat = await store.get_chat(chat.id) or chat
                    from_peer2, msg_id2 = post.pick_forward(want_photo=False)  # type: ignore[misc]
                    assert from_peer2 is not None
                    await log.emit(
                        f"{account.label}: «{chat.title}»: медиа нельзя — "
                        f"переключаюсь на текстовую ссылку"
                    )
                    result = await schedule_chat_forwards(
                        client=client,
                        target=entity,
                        from_peer=from_peer2,
                        message_id=msg_id2,
                        start_minute=minute,
                        interval_minutes=interval_min,
                        start_hour=settings.start_hour,
                        tz=settings.tz,
                        repeat_period=repeat_period,
                    )
        else:
            photo = photo_src
            if not want_media:
                photo = None
            result = await schedule_chat_posts(
                client=client,
                target=entity,
                text=text,
                entities=entities,
                start_minute=minute,
                interval_minutes=interval_min,
                start_hour=settings.start_hour,
                tz=settings.tz,
                repeat_period=repeat_period,
                photo_path=photo,
                allow_media=want_media,
            )
            if result.get("media_blocked") and want_media:
                await store.update_chat(chat.id, allow_media=0)
                await log.emit(
                    f"{account.label}: «{chat.title}»: фото запрещено — дальше только текст"
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
            f"{account.label}: Ошибка schedule «{chat.title}»: "
            f"{type(e).__name__}: {e}",
            "error",
        )
        await store.record_setup_fail(
            account.id, chat.id, f"{type(e).__name__}: {e}", max_attempts=max_attempts
        )
        return "skipped"

    if result["success"] < result["planned"]:
        err = result["error"] or f"{result['success']}/{result['planned']}"
        # Бан / нет прав писать — как «недоступен», а не «частично»
        if is_unavailable_text(err):
            return await _fail_unavailable(
                store,
                account,
                chat,
                err,
                log,
                max_attempts=max_attempts,
            )
        await log.emit(
            f"{account.label}: Частично «{chat.title}»: "
            f"{result['success']}/{result['planned']}"
            + (f" | {result['error']}" if result["error"] else ""),
            "error",
        )
        await store.record_setup_fail(
            account.id, chat.id, err, max_attempts=max_attempts
        )
        return "skipped"

    await store.record_setup_ok(account.id, chat.id)
    tag_note = f" | тег {chat.tag}" if chat.tag.strip() else ""
    mode_note = " | forward" if result.get("mode") == "forward" else ""
    photo_note = ""
    if result.get("mode") != "forward":
        photo_note = " | фото" if result.get("used_photo") else " | без фото"
    repeat_note = (
        f" | repeat {repeat_period}s"
        if repeat_period
        else " | без repeat (не Premium — суточный cron)"
    )
    await log.emit(
        f"На аккаунт {account.label} настроил отправку на чат "
        f"«{result['title']}» | :{minute:02d} | "
        f"{result['success']} слотов | старт {result['first_time']}"
        f"{tag_note}{mode_note}{photo_note}{repeat_note}"
        + (f" | очистил {result['cleared']} старых" if result["cleared"] else ""),
        notify=False,
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
    is_premium: bool = False,
) -> dict[str, list[str]]:
    """Schedule many chats; never aborts the whole batch on a missing chat."""
    buckets: dict[str, list[str]] = {
        "ok": [],
        "skipped": [],
        "abandoned": [],
        "config_error": [],
    }
    for chat in chats:
        # setup:cancel по account.id; глобальные jobs (fix_minutes и т.п.) — :0
        if runtime.cancelled(job_kind, account.id) or runtime.cancelled(job_kind, 0):
            raise SetupError("Остановлено")
        outcome = await schedule_one_chat(
            store,
            client,
            account,
            chat,
            posts,
            log,
            skip_abandoned=skip_abandoned,
            is_premium=is_premium,
        )
        if outcome == "stopped":
            raise SetupError("Остановлено")
        buckets.setdefault(outcome, []).append(chat.title)
    return buckets


async def _configure_sender(
    store: Store,
    account: Account,
    posts: dict[str, Post],
    schedule_chats: list[Chat],
    sender_chats: list[Chat],
    log: LogSink,
) -> int:
    """Configure Autoposter for this account only (не трогаем другие аккаунты)."""
    settings = get_settings()
    api = SenderAPI(settings.sender_api_url, settings.sender_api_key)
    load = await account_load(store, account)
    stop_reason = (
        "аутрич: sender не используется, только schedule"
        if account.is_outreach
        else "dead-режим (sender выключен навсегда, работает только schedule)"
        if account.is_dead
        else "sender выключен вручную"
        if not account.sender_on
        else f"спамблок: нагрузка {load}% — sender остановлен"
        if load <= 0
        else ""
    )
    if stop_reason:
        sid = (account.sender_account_id or "").strip()
        if sid:
            try:
                await api.spam_stop(sid)
            except SenderAPIError as e:
                await log.emit(
                    f"{account.label}: sender остановлен ({stop_reason}), но стоп не прошёл: {e}",
                    "error",
                )
        await store.set_applied_load(account.id, load)
        await log.emit(f"{account.label}: {stop_reason} — настройку sender пропускаю")
        return 0
    keywords = await store.get_stoplist()
    sender_id = await ensure_sender_account(store, account, api, log)
    prefs = await store.prefs_for_account(account.id)
    banned_pks = {b.chat_pk for b in await store.list_bans(account_id=account.id)}
    ss = await store.sender_settings()

    ru_text, ru_ents = render_post(
        posts["ru"].text,
        entities_loads(posts["ru"].entities_json),
        None,
    )
    photo_bytes = None
    if posts["ru"].photo_path and Path(posts["ru"].photo_path).exists():
        photo_bytes = Path(posts["ru"].photo_path).read_bytes()

    push = await push_sender_post(
        api,
        sender_id,
        ru_text,
        ru_ents,
        photo_bytes,
        telethon_session=account.telethon_session or None,
    )
    mode = push.get("mode") or "post"
    await log.emit(
        f"{account.label}: настроил пост RU "
        f"(mode={mode}, premium emoji="
        f"{'да' if has_premium_emoji(ru_ents) else 'нет'}, "
        f"переносов={ru_text.count(chr(10))})"
    )

    # Спамблок/восстановление: интервалы растягиваются (50% → вдвое реже), параллель режется.
    between = (scale_seconds(ss.between_min, load), scale_seconds(ss.between_max, load))
    cycle = (scale_seconds(ss.cycle_min, load), scale_seconds(ss.cycle_max, load))
    per_chat = (scale_seconds(ss.per_chat_min, load), scale_seconds(ss.per_chat_max, load))
    parallel = scale_parallel(ss.parallel, load)
    await api.put_interval(sender_id, "between", *between)
    await api.put_interval(sender_id, "cycle", *cycle)
    await api.put_interval(sender_id, "per_chat", *per_chat)
    await api.put_parallel(sender_id, parallel)
    await log.emit(
        f"{account.label}: настроил интервалы (нагрузка {load}%) "
        f"between {between[0]}-{between[1]}s | "
        f"cycle {cycle[0]}-{cycle[1]}s | "
        f"per-chat {per_chat[0]}-{per_chat[1]}s | "
        f"parallel={parallel}"
    )

    # Упоминания / «глобальная отметка» — по настройке Sender (по умолчанию выкл).
    live_chats = await api.list_chats(sender_id)
    mentions_on = bool(getattr(ss, "mentions_enabled", False))
    ment_n = await apply_mentions_everywhere(
        api, sender_id, enabled=mentions_on, live_chats=live_chats
    )
    await log.emit(
        f"{account.label}: упоминания "
        f"{'вкл (global)' if mentions_on else 'выкл'} "
        f"(аккаунт + {ment_n} чатов)"
    )

    # Клоакинг — полный пуш на этот аккаунт (без apply-all).
    cloak_text = normalize_multiline(ss.cloak_text)
    cloak_on = bool(ss.cloak_enabled and cloak_text.strip())
    cloak_res = await push_cloak(
        api, sender_id, enabled=cloak_on, text=cloak_text
    )
    if cloak_on and not cloak_res.get("ok"):
        await log.emit(
            f"{account.label}: клоакинг не подтвердился в Autoposter — повторяю",
            "error",
        )
        cloak_res = await push_cloak(
            api, sender_id, enabled=True, text=cloak_text
        )
    await log.emit(
        f"{account.label}: клоакинг "
        f"{'вкл' if cloak_res.get('enabled') else 'выкл'}"
        + (
            f" ({cloak_res.get('text_len', 0)} симв."
            f", ok={cloak_res.get('ok')})"
            if cloak_res.get("enabled")
            else ""
        )
    )

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

    sender_count = 0
    disabled_n = 0
    for live in targets:
        if runtime.cancelled("setup", account.id):
            raise SetupError("Остановлено")
        cid = str(live.get("chat_id"))
        catalog = match_catalog_chat(cid, sender_chats)
        pref = prefs.get(catalog.id) if catalog else None
        if catalog:
            allowed, why = is_send_allowed(
                account,
                catalog,
                pref,
                catalog.id in banned_pks,
                spam_load=load,
                stoplist=keywords,
            )
            if not allowed:
                try:
                    await api.patch_chat(sender_id, cid, active=False)
                except SenderAPIError as e:
                    await log.emit(
                        f"Sender: не удалось выключить «{catalog.title}»: {e}", "error"
                    )
                disabled_n += 1
                await log.emit(
                    f"Sender: «{catalog.title}» выключен для {account.label} ({why})",
                    notify=False,
                )
                continue
        if matches_stoplist(str(live.get("title") or ""), keywords):
            # чат не в каталоге (или в каталоге с другим названием), но писать туда нельзя
            try:
                await api.patch_chat(sender_id, cid, active=False)
            except SenderAPIError as e:
                await log.emit(f"Sender: не удалось выключить «{live.get('title')}»: {e}", "error")
            disabled_n += 1
            await log.emit(
                f"Sender: «{live.get('title')}» в стоп-листе — писать нельзя, выключил",
                notify=False,
            )
            continue
        override = pick_text_override(pref)
        if catalog:
            post = pick_post_for_chat(posts, catalog)
            tag = catalog.tag
            title = catalog.title or live.get("title") or cid
            kind_note = "кор." if catalog.uses_short_text else "полн."
        else:
            post = posts.get("ru") or next(iter(posts.values()))
            tag = None
            title = live.get("title") or cid
            kind_note = "полн."
        if override is not None:
            text, ents = render_post(override, entities_loads(pref.entities_json), tag)
            kind_note = "свой текст"
        else:
            text, ents = render_post(
                post.text,
                entities_loads(post.entities_json),
                tag,
            )
        await push_chat_text(
            api, sender_id, cid, text, ents, mentions_enabled=mentions_on
        )
        if catalog and int(catalog.max_posts_per_account or 0) > 0:
            try:
                await api.patch_chat(
                    sender_id,
                    cid,
                    interval=scale_seconds(effective_interval_seconds(catalog), load),
                )
                kind_note += f", лимит {catalog.max_posts_per_account}/сут"
            except SenderAPIError as e:
                await log.emit(
                    f"Sender: лимит постов для «{title}» не применился: {e}", "error"
                )
        sender_count += 1
        await log.emit(
            f"Sender: включил «{title}» ({kind_note}, "
            f"mention={'on' if mentions_on else 'off'})",
            notify=False,
        )

    if sender_count:
        await api.spam_start(sender_id)
        # spam/start включает spam_enabled у всех — снова глушим schedule
        live_chats = await api.list_chats(sender_id)
        await _mute_schedule_on_sender(api, sender_id, live_chats, schedule_chats)
        # после старта снова глушим отметки (на случай дефолтов Autoposter)
        await apply_mentions_everywhere(
            api, sender_id, enabled=mentions_on, live_chats=live_chats
        )
        # и снова клоакинг — spam/start не должен его сбрасывать, но проверяем
        cloak_res = await push_cloak(
            api, sender_id, enabled=cloak_on, text=cloak_text
        )
        await log.emit(
            f"{account.label}: sender запущен на {sender_count} чатов"
            f"{f' (выключено вручную/баном: {disabled_n})' if disabled_n else ''} "
            f"(schedule mute, mention=off, cloak="
            f"{'on' if cloak_res.get('enabled') else 'off'})"
        )
    else:
        await log.emit(
            f"{account.label}: sender — нет чатов кроме schedule, spam/start не вызываю"
        )
    await store.set_applied_load(account.id, load)
    return sender_count


async def run_setup(store: Store, account_id: int, bot: Bot, admin_chat_id: int) -> None:
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
    schedule_failed = False

    try:
        posts = {
            "ru": await store.get_post(account.id, "ru"),
            "en": await store.get_post(account.id, "en"),
            "ru_short": await store.get_post(account.id, "ru_short"),
            "en_short": await store.get_post(account.id, "en_short"),
        }
        has_any = any(
            (p.text or "").strip()
            or p.has_text_link
            or p.has_photo_link
            for p in posts.values()
        )
        if not has_any:
            raise SetupError("Сначала задайте пост или ссылку (RU/EN)")

        chats = await store.list_chats(enabled_only=True)
        schedule_chats = [c for c in chats if c.is_schedule]
        sender_chats = [c for c in chats if c.kind == "sender"]

        if not schedule_chats and not account.has_sender:
            raise SetupError("Нет включённых schedule-чатов и sender не готов")

        await log.emit(
            f"Старт настройки {account.display}\n"
            f"Schedule-чатов: {len(schedule_chats)} | "
            f"Sender: {'все остальные диалоги' if account.has_sender else 'нет'}"
        )

        for chat in schedule_chats:
            await ensure_minute(store, chat, account)

        if schedule_chats:
            if not account.telethon_session:
                schedule_failed = True
                await log.emit(
                    "Нет Telethon session — schedule пропускаю, перехожу к sender",
                    "error",
                )
            else:
                try:
                    async with telethon_client(account.telethon_session) as client:
                        me = await client.get_me()
                        is_premium = bool(getattr(me, "premium", False))
                        await store.update_account(
                            account.id, is_premium=1 if is_premium else 0
                        )
                        account = await store.get_account(account.id) or account
                        await log.emit(
                            f"Telethon: {me.first_name} "
                            f"(@{me.username or '-'}) id={me.id}"
                            f"{' | Premium' if is_premium else ' | без Premium'}"
                        )
                        if not is_premium:
                            await log.emit(
                                "Без Premium: schedule без schedule_repeat_period; "
                                "суточный cron переназначит слоты"
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
                            is_premium=is_premium,
                        )
                        scheduled_ok = buckets.get("ok", [])
                        scheduled_skip = buckets.get("skipped", []) + buckets.get(
                            "config_error", []
                        )
                        scheduled_abandoned = buckets.get("abandoned", [])
                except SetupError:
                    raise
                except Exception as e:
                    schedule_failed = True
                    await log.emit(
                        f"Schedule оборвался ({type(e).__name__}: {e}) — "
                        f"sender всё равно настрою, если готов",
                        "error",
                    )

        if account.has_sender:
            try:
                sender_count = await _configure_sender(
                    store,
                    account,
                    posts,
                    schedule_chats,
                    sender_chats,
                    log,
                )
            except SetupError:
                raise
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                await log.emit(f"Ошибка sender {account.label}: {err}", "error")
                if not scheduled_ok and schedule_failed:
                    raise SetupError(f"Schedule и sender не настроены: {err}") from e
                raise SetupError(f"Sender не настроен: {err}") from e
        elif sender_chats:
            await log.emit(
                f"{account.label}: в каталоге есть sender-чаты, но sender не готов — пропускаю",
                "error",
            )

        report_lines.append(f"Аккаунт: {account.display}")
        if account.has_telethon:
            report_lines.append(
                "Premium: да"
                if account.has_premium
                else "Premium: нет (без repeat, суточный cron)"
            )
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
    """Schedule only selected chats for an account (used by retry / weekly / daily)."""
    account = await store.get_account(account_id)
    empty = {"ok": [], "skipped": [], "abandoned": [], "config_error": []}
    if not account or not account.telethon_session or not chat_pks:
        return empty

    posts = {
        "ru": await store.get_post(account.id, "ru"),
        "en": await store.get_post(account.id, "en"),
        "ru_short": await store.get_post(account.id, "ru_short"),
        "en_short": await store.get_post(account.id, "en_short"),
    }
    has_any = any(
        (p.text or "").strip() or p.has_text_link or p.has_photo_link for p in posts.values()
    )
    if not has_any:
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
            me = await client.get_me()
            is_premium = bool(getattr(me, "premium", False))
            await store.update_account(account.id, is_premium=1 if is_premium else 0)
            account = await store.get_account(account.id) or account
            buckets = await schedule_chats_batch(
                store,
                client,
                account,
                chats,
                posts,
                log,
                skip_abandoned=skip_abandoned,
                job_kind=job_kind,
                is_premium=is_premium,
            )
        report = (
            f"{account.label}: ok={len(buckets.get('ok', []))} "
            f"skip={len(buckets.get('skipped', []))} "
            f"abandoned={len(buckets.get('abandoned', []))} "
            f"premium={int(account.has_premium)}"
        )
        await store.finish_job(job.id, "done", report)
        return buckets
    except SetupError as e:
        await store.finish_job(job.id, "cancelled" if str(e) == "Остановлено" else "error", str(e))
        return empty
    except Exception as e:
        await store.finish_job(job.id, "error", f"{type(e).__name__}: {e}")
        return empty


async def run_sender_refresh(
    store: Store,
    account_id: int,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    job_kind: str = "sender_refresh",
) -> int:
    """Перенастроить только sender у аккаунта (без пересборки schedule)."""
    account = await store.get_account(account_id)
    if not account or not account.has_sender:
        return 0
    posts = {
        "ru": await store.get_post(account.id, "ru"),
        "en": await store.get_post(account.id, "en"),
        "ru_short": await store.get_post(account.id, "ru_short"),
        "en_short": await store.get_post(account.id, "en_short"),
    }
    has_any = any(
        (p.text or "").strip() or p.has_text_link or p.has_photo_link for p in posts.values()
    )
    job = await store.create_job(job_kind, account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    if not has_any:
        await store.finish_job(job.id, "error", "нет поста")
        await log.emit(f"{account.label}: sender не настроен — задайте пост", "error")
        return 0
    try:
        chats = await store.list_chats(enabled_only=True)
        schedule_chats = [c for c in chats if c.is_schedule]
        sender_chats = [c for c in chats if c.kind == "sender"]
        count = await _configure_sender(
            store, account, posts, schedule_chats, sender_chats, log
        )
        await store.finish_job(job.id, "done", f"sender: {count} чатов")
        return count
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        await store.finish_job(job.id, "error", err)
        await log.emit(f"{account.label}: sender не настроен — {err}", "error")
        return 0
