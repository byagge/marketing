from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aiogram import Bot
from telethon import TelegramClient

from app.config import get_settings
from app.jobs import LogSink, runtime
from app.models import Account, Chat, Post
from app.sender_api import SenderAPI, SenderAPIError
from app.store import Store
from app.tg.client import peer_id, telethon_client
from app.tg.resolve import lookup_entity
from app.tg.scheduler import (
    is_media_forbidden_error,
    peer_allows_photos,
    schedule_chat_forwards,
    schedule_chat_posts,
)
from app.tg.restrictions import classify_text, record_restriction
from app.tg.unavailable import format_unavailable, is_chat_unavailable, is_unavailable_text
from app.utils.chat_ids import canon_chat_id, chat_id_aliases, chat_ids_match
from app.utils.timefmt import fmt_until
from app.utils.entities import entities_loads
from app.utils.minutes import period_for_interval, suggest_minute
from app.utils.schedule import dead_interval, phase_offset_minutes, resolve_repeat_period
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


# Эти задачи пересобирают расписание часто — для них доливаем недостающее, а не сносим всё.
SYNC_JOB_KINDS = frozenset({"setup_daily_nonpremium", "rebalance"})


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


async def _blocked_chat_keys(store: Store, account_id: int) -> set[str]:
    """Каталожные чаты, где у аккаунта активный бан/мут (ключ canon chat id)."""
    keys: set[str] = set()
    restrictions = await store.list_restrictions(account_id=account_id)
    for restr in restrictions:
        chat = await store.get_chat(restr.chat_pk)
        if chat:
            keys.add(canon_chat_id(chat.chat_id))
    return keys


async def apply_chat_prefs(
    api: SenderAPI,
    sender_id: str,
    live_chats: list[dict],
    prefs: dict,
    blocked_keys: set[str] | None = None,
) -> int:
    """Ручное выкл/вкл чатов и отметки по чатам — поверх общих настроек."""
    blocked_keys = blocked_keys or set()
    changed = 0
    for live in live_chats:
        cid = str(live.get("chat_id") or "")
        if not cid:
            continue
        key = canon_chat_id(cid)
        pref = prefs.get(key)
        try:
            if (pref is not None and not pref.is_enabled) or key in blocked_keys:
                await api.patch_chat(sender_id, cid, active=False)
                changed += 1
            elif pref is not None and pref.mention >= 0:
                await api.patch_chat(
                    sender_id, cid, mention=True if pref.mention else False
                )
                changed += 1
        except SenderAPIError:
            continue
    return changed


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


_KIND_LABEL = {
    "ban": "БАН — записан в базу банов, больше не пробую",
    "mute": "мут — записан в базу мутов",
    "nowrite": "запрет писать — записан в базу мутов",
    "spamblock": "SpamBlock аккаунта — в этот чат писать нельзя, попробую позже",
}


async def _fail_unavailable(
    store: Store,
    account: Account,
    chat: Chat,
    error: str,
    log: LogSink,
    *,
    max_attempts: int,
    client: TelegramClient | None = None,
    entity: Any = None,
) -> str:
    # бан / мут → отдельная база (по ним больше не пытаемся вступать и писать)
    hint = classify_text(error)
    prev_restr = await store.get_restriction(account.id, chat.id)
    kind: str | None = None
    if hint:
        try:
            kind = await record_restriction(
                store, client, account, chat, entity, hint=hint, error=error
            )
        except Exception:  # noqa: BLE001
            kind = hint
    prev_state = await store.get_setup_state(account.id, chat.id)
    was_abandoned = bool(prev_state and prev_state.is_abandoned)
    state = await store.record_setup_fail(
        account.id, chat.id, error, max_attempts=max_attempts
    )
    prefix = f"{account.label}: "
    # повторные одинаковые ошибки не шлём в Telegram (раньше — 21 сообщение в час)
    new_restr = kind is not None and (prev_restr is None or prev_restr.kind != kind)
    notify = (not was_abandoned) or new_restr

    if kind:
        restr = await store.get_restriction(account.id, chat.id)
        extra = ""
        if restr and (restr.is_mute or restr.is_spamblock):
            extra = f" до {fmt_until(restr.until_at)}"
            if restr.reason:
                extra += f" | причина: {restr.reason[:200]}"
        await log.emit(
            f"{prefix}Пропуск «{chat.title}»: {_KIND_LABEL.get(kind, kind)}{extra}",
            "error",
            notify=notify,
        )
        return "abandoned" if kind == "ban" or state.is_abandoned else "skipped"

    if state.is_abandoned:
        await log.emit(
            f"{prefix}Пропуск «{chat.title}»: недоступен ({error}). "
            f"Попытка {state.fail_count}/{max_attempts} — больше не пробую "
            f"до начала недели",
            "error",
            notify=notify,
        )
        return "abandoned"
    await log.emit(
        f"{prefix}Пропуск «{chat.title}»: недоступен ({error}). "
        f"Попытка {state.fail_count}/{max_attempts}, повтор через "
        f"{get_settings().setup_retry_days} дн.",
        "error",
    )
    return "skipped"


async def pair_blocked(store: Store, account: Account, chat: Chat) -> str | None:
    """Почему эту пару не настраиваем: disabled | banned | muted | None."""
    pref = await store.get_pref(account.id, chat.chat_id)
    if not pref.is_enabled:
        return "disabled"
    restr = await store.get_restriction(account.id, chat.id)
    if restr is not None:
        if restr.is_ban:
            return "banned"
        return "spamblock" if restr.is_spamblock else "muted"
    return None


def schedule_signature(**parts: Any) -> str:
    """Отпечаток всего, что влияет на содержимое и время scheduled-сообщений."""
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _photo_stamp(path: str | None) -> list[Any] | None:
    if not path:
        return None
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return [path, None, None]
    return [path, st.st_size, int(st.st_mtime)]


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
    dead: bool = False,
    entity: Any | None = None,
    sync: bool = False,
) -> str:
    """
    Schedule posts for one chat.

    sync=True — если настройки (текст, минута, интервал…) не менялись с прошлой
    успешной сборки, не пересоздаём всё, а доливаем только недостающие слоты.

    dead=True — расходный аккаунт: свой плотный интервал, без минутного слота,
    без setup_states/abandoned, ошибки молча → skipped. entity можно передать
    готовый (уже найденный среди диалогов аккаунта).

    Returns: ok | skipped | abandoned | config_error | stopped
    """
    settings = get_settings()
    max_attempts = settings.setup_max_attempts

    if not dead:
        blocked = await pair_blocked(store, account, chat)
        if blocked:
            # выключено вручную / бан / мут — тихо пропускаем, в базе всё видно
            await log.emit(
                f"{account.label}: «{chat.title}» пропущен ({blocked})", notify=False
            )
            return blocked

    if skip_abandoned and not dead:
        state = await store.get_setup_state(account.id, chat.id)
        if state and state.is_abandoned:
            await log.emit(
                f"{account.label}: Пропуск «{chat.title}»: уже {state.fail_count} "
                f"неудачных попыток (ждём начало недели)",
                "error",
                notify=False,
            )
            return "abandoned"

    post = pick_post_for_chat(posts, chat)
    custom = await store.get_chat_post(account.id, chat.chat_id)
    if custom is not None:
        post = custom  # свой текст для этого чата у этого аккаунта
    use_link = post.is_link_mode and (post.has_text_link or post.has_photo_link)
    if not use_link and not post.text.strip():
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
    else:
        text, entities = render_post(
            post.text,
            entities_loads(post.entities_json),
            chat.tag,
        )
    interval = chat.interval_minutes
    offset = 0
    if dead:
        interval = dead_interval(chat.interval_minutes, settings.dead_interval_minutes)
        minute = (account.id * 7) % 60
    else:
        slot = await store.slot_for(chat.id, account.id)
        minute = slot.start_minute if slot else await ensure_minute(store, chat, account)
        if chat.interval_minutes > 60:
            slots = sorted(
                await store.slots_for_chat(chat.id),
                key=lambda sl: (int(sl.start_minute), int(sl.account_id)),
            )
            rank = next(
                (i for i, sl in enumerate(slots) if sl.account_id == account.id), 0
            )
            offset = phase_offset_minutes(rank, chat.interval_minutes)
    repeat_period = resolve_repeat_period(is_premium, settings.repeat_period)
    extra_hours = 0.0 if repeat_period else float(settings.nonpremium_extra_hours)
    prior = await store.get_setup_state(account.id, chat.id) if (sync and not dead) else None
    fallback_used = False

    def _sync_for(sig: str) -> bool:
        return bool(
            sync and not dead and prior and prior.status == "ok" and prior.sig and prior.sig == sig
        )

    sig = ""
    try:
        if entity is None:
            entity = await lookup_entity(client, chat)
        if entity is None:
            if dead:
                return "skipped"
            return await _fail_unavailable(
                store,
                account,
                chat,
                "чат не найден в аккаунте",
                log,
                max_attempts=max_attempts,
                client=client,
            )
        # заранее: если в чат нельзя фото — сразу без медиа (caption/текст)
        want_media = chat.media_allowed
        if want_media and (
            (use_link and post.has_photo_link)
            or (not use_link and bool(post.photo_path))
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
            sig = schedule_signature(
                mode="forward", from_peer=str(from_peer), msg_id=int(msg_id),
                interval=interval, minute=minute, offset=offset,
                repeat=bool(repeat_period), hour=settings.start_hour,
                extra=extra_hours,
            )
            result = await schedule_chat_forwards(
                client=client,
                target=entity,
                from_peer=from_peer,
                message_id=msg_id,
                start_minute=minute,
                interval_minutes=interval,
                start_hour=settings.start_hour,
                tz=settings.tz,
                repeat_period=repeat_period,
                rolling=repeat_period is None,
                offset_minutes=offset,
                sync=_sync_for(sig),
                extra_hours=extra_hours,
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
                    fallback_used = True
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
                        interval_minutes=interval,
                        start_hour=settings.start_hour,
                        tz=settings.tz,
                        repeat_period=repeat_period,
                        rolling=repeat_period is None,
                        offset_minutes=offset,
                    )
        else:
            photo = post.photo_path or None
            if not want_media:
                photo = None
            sig = schedule_signature(
                mode="post", text=text, entities=entities, photo=_photo_stamp(photo),
                allow_media=want_media, interval=interval, minute=minute,
                offset=offset, repeat=bool(repeat_period), hour=settings.start_hour,
                extra=extra_hours,
            )
            result = await schedule_chat_posts(
                client=client,
                target=entity,
                text=text,
                entities=entities,
                start_minute=minute,
                interval_minutes=interval,
                start_hour=settings.start_hour,
                tz=settings.tz,
                repeat_period=repeat_period,
                photo_path=photo,
                allow_media=want_media,
                rolling=repeat_period is None,
                offset_minutes=offset,
                sync=_sync_for(sig),
                extra_hours=extra_hours,
            )
            if result.get("media_blocked") and want_media:
                fallback_used = True
                await store.update_chat(chat.id, allow_media=0)
                await log.emit(
                    f"{account.label}: «{chat.title}»: фото запрещено — дальше только текст"
                )
    except Exception as e:
        if dead:
            await log.emit(
                f"{account.label}: dead: «{chat.title}» пропущен "
                f"({type(e).__name__}: {e})",
                notify=False,
            )
            return "skipped"
        if is_chat_unavailable(e):
            return await _fail_unavailable(
                store,
                account,
                chat,
                format_unavailable(e),
                log,
                max_attempts=max_attempts,
                client=client,
                entity=locals().get("entity"),
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

    if dead:
        # Расходник: что успели запланировать — то и ладно, без счётчиков отказов.
        await log.emit(
            f"{account.label}: dead: «{chat.title}» {result['success']}/"
            f"{result['planned']} слотов, каждые {interval} мин"
            + (f" | {result['error']}" if result["error"] else ""),
            notify=False,
        )
        return "ok" if result["success"] > 0 else "skipped"

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
                client=client,
                entity=locals().get("entity"),
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

    # при откате на текст/ссылку фактический контент ≠ sig → пусть следующий раз соберёт заново
    await store.record_setup_ok(account.id, chat.id, "" if fallback_used else sig)
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
    dead: bool = False,
    entities: dict[int, Any] | None = None,
    sync: bool = False,
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
            dead=dead,
            entity=(entities or {}).get(chat.id),
            sync=sync,
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
    sender_id = await ensure_sender_account(store, account, api, log)
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

    await api.put_interval(sender_id, "between", ss.between_min, ss.between_max)
    await api.put_interval(sender_id, "cycle", ss.cycle_min, ss.cycle_max)
    await api.put_interval(sender_id, "per_chat", ss.per_chat_min, ss.per_chat_max)
    await api.put_parallel(sender_id, ss.parallel)
    await log.emit(
        f"{account.label}: настроил интервалы "
        f"between {ss.between_min}-{ss.between_max}s | "
        f"cycle {ss.cycle_min}-{ss.cycle_max}s | "
        f"per-chat {ss.per_chat_min}-{ss.per_chat_max}s | "
        f"parallel={ss.parallel}"
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
    prefs = {p.chat_key: p for p in await store.list_prefs(account.id)}
    blocked_keys = await _blocked_chat_keys(store, account.id)
    for chat in sender_chats:
        if not any(chat_ids_match(chat.chat_id, cid) for cid in live_ids):
            await log.emit(
                f"Sender: чат каталога «{chat.title}» ({chat.chat_id}) "
                f"не найден в диалогах аккаунта — пропускаю",
                "error",
            )

    sender_count = 0
    for live in targets:
        if runtime.cancelled("setup", account.id):
            raise SetupError("Остановлено")
        cid = str(live.get("chat_id"))
        key = canon_chat_id(cid)
        pref = prefs.get(key)
        if (pref is not None and not pref.is_enabled) or key in blocked_keys:
            # выключено вручную / бан / мут: в Autoposter чат не включаем
            try:
                await api.patch_chat(sender_id, cid, active=False)
            except SenderAPIError:
                pass
            await log.emit(
                f"Sender: «{live.get('title') or cid}» выключен "
                f"({'вручную' if pref is not None and not pref.is_enabled else 'бан/мут'})",
                notify=False,
            )
            continue
        catalog = match_catalog_chat(cid, sender_chats)
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
        custom = await store.get_chat_post(account.id, cid)
        if custom is not None:
            post = custom
            kind_note = "свой"
        text, ents = render_post(
            post.text,
            entities_loads(post.entities_json),
            tag,
        )
        chat_mentions = (
            mentions_on if pref is None or pref.mention < 0 else bool(pref.mention)
        )
        await push_chat_text(
            api, sender_id, cid, text, ents, mentions_enabled=chat_mentions
        )
        sender_count += 1
        await log.emit(
            f"Sender: включил «{title}» ({kind_note}, "
            f"mention={'on' if chat_mentions else 'off'})",
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
        await apply_chat_prefs(api, sender_id, live_chats, prefs, blocked_keys)
        # и снова клоакинг — spam/start не должен его сбрасывать, но проверяем
        cloak_res = await push_cloak(
            api, sender_id, enabled=cloak_on, text=cloak_text
        )
        await log.emit(
            f"{account.label}: sender запущен на {sender_count} чатов "
            f"(schedule mute, mention=off, cloak="
            f"{'on' if cloak_res.get('enabled') else 'off'})"
        )
    else:
        await log.emit(
            f"{account.label}: sender — нет чатов кроме schedule, spam/start не вызываю"
        )
    return sender_count


async def run_setup(store: Store, account_id: int, bot: Bot, admin_chat_id: int) -> None:
    account = await store.get_account(account_id)
    if not account:
        return
    if account.is_dead:
        await run_dead_setup(store, account_id, bot, admin_chat_id)
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
    if account.is_dead:
        # Расходник: игнорируем список chat_pks, шлём во все доступные чаты.
        return await run_dead_schedule(
            store, account_id, bot, admin_chat_id, job_kind=job_kind
        )

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
                # ночная пересборка/перераспределение: не трогаем то, что уже стоит как надо
                sync=job_kind in SYNC_JOB_KINDS,
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


async def member_schedule_chats(
    client: TelegramClient, chats: list[Chat]
) -> dict[int, Any]:
    """
    Чаты каталога, в которых аккаунт реально состоит (по его диалогам).
    Один проход по диалогам, без get_entity на каждый чат → без лишних FloodWait.
    Возвращает chat.id → entity.
    """
    by_alias: dict[str, Any] = {}
    by_username: dict[str, Any] = {}
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        for alias in chat_id_aliases(peer_id(ent)) | chat_id_aliases(
            getattr(ent, "id", None)
        ):
            by_alias.setdefault(alias, ent)
        uname = (getattr(ent, "username", None) or "").strip().casefold()
        if uname:
            by_username[uname] = ent

    found: dict[int, Any] = {}
    for chat in chats:
        ent = None
        for alias in chat_id_aliases(chat.chat_id):
            ent = by_alias.get(alias)
            if ent is not None:
                break
        if ent is None and chat.username:
            ent = by_username.get(chat.username.strip().lstrip("@").casefold())
        if ent is not None:
            found[chat.id] = ent
    return found


async def run_dead_schedule(
    store: Store,
    account_id: int,
    bot: Bot | None,
    admin_chat_id: int | None,
    *,
    job_kind: str = "setup_dead",
) -> dict[str, list[str]]:
    """
    Dead-аккаунт: слать максимум во все schedule-чаты, где он уже состоит.

    Без проверок и счётчиков отказов: нет чата / бан / мут → молча пропускаем.
    Минутные слоты не занимаем (сетка живых аккаунтов не страдает), уведомлений
    админу не шлём (LogSink без бота) — итог только в job_logs.
    """
    account = await store.get_account(account_id)
    empty: dict[str, list[str]] = {
        "ok": [], "skipped": [], "abandoned": [], "config_error": []
    }
    if not account or not account.telethon_session:
        return empty

    posts = {
        "ru": await store.get_post(account.id, "ru"),
        "en": await store.get_post(account.id, "en"),
        "ru_short": await store.get_post(account.id, "ru_short"),
        "en_short": await store.get_post(account.id, "en_short"),
    }
    if not any(
        (p.text or "").strip() or p.has_text_link or p.has_photo_link
        for p in posts.values()
    ):
        return empty

    schedule_chats = [
        c for c in await store.list_chats(enabled_only=True) if c.is_schedule
    ]
    if not schedule_chats:
        return empty

    job = await store.create_job(job_kind, account.id)
    log = LogSink(store, job.id)  # без бота — тишина
    try:
        async with telethon_client(account.telethon_session) as client:
            me = await client.get_me()
            is_premium = bool(getattr(me, "premium", False))
            await store.update_account(account.id, is_premium=1 if is_premium else 0)
            account = await store.get_account(account.id) or account
            member = await member_schedule_chats(client, schedule_chats)
            targets = [c for c in schedule_chats if c.id in member]
            buckets = await schedule_chats_batch(
                store,
                client,
                account,
                targets,
                posts,
                log,
                job_kind=job_kind,
                is_premium=is_premium,
                dead=True,
                entities=member,
            )
            buckets["skipped"] = buckets.get("skipped", []) + [
                c.title for c in schedule_chats if c.id not in member
            ]
        await store.finish_job(
            job.id,
            "done",
            f"{account.label} [dead]: ok={len(buckets['ok'])} "
            f"skip={len(buckets['skipped'])} premium={int(account.has_premium)}",
        )
        return buckets
    except SetupError as e:
        await store.finish_job(
            job.id, "cancelled" if str(e) == "Остановлено" else "error", str(e)
        )
        return empty
    except Exception as e:
        await store.finish_job(job.id, "error", f"{type(e).__name__}: {e}")
        return empty


async def run_dead_setup(
    store: Store,
    account_id: int,
    bot: Bot | None,
    admin_chat_id: int | None,
) -> None:
    """Ручной «Старт» для dead-аккаунта: schedule везде, где состоит + sender best-effort."""
    account = await store.get_account(account_id)
    if not account:
        return
    await store.update_account(account.id, status="running", last_error="")
    try:
        buckets = await run_dead_schedule(
            store, account_id, bot, admin_chat_id, job_kind="setup"
        )
        sender_note = ""
        if account.has_sender:
            job = await store.create_job("setup_dead_sender", account.id)
            log = LogSink(store, job.id)
            try:
                chats = await store.list_chats(enabled_only=True)
                posts = {
                    "ru": await store.get_post(account.id, "ru"),
                    "en": await store.get_post(account.id, "en"),
                    "ru_short": await store.get_post(account.id, "ru_short"),
                    "en_short": await store.get_post(account.id, "en_short"),
                }
                n = await _configure_sender(
                    store,
                    account,
                    posts,
                    [c for c in chats if c.is_schedule],
                    [c for c in chats if c.kind == "sender"],
                    log,
                )
                sender_note = f" | sender: {n} чатов"
                await store.finish_job(job.id, "done", sender_note)
            except Exception as e:
                sender_note = " | sender: не вышло (игнор)"
                await store.finish_job(job.id, "error", f"{type(e).__name__}: {e}")
        await store.update_account(account.id, status="done", last_error="")
        if bot and admin_chat_id:
            try:
                await bot.send_message(
                    admin_chat_id,
                    f"\U0001F480 {account.label} [dead]: schedule в "
                    f"{len(buckets['ok'])} чатах, недоступно/пропущено "
                    f"{len(buckets['skipped'])}{sender_note}",
                )
            except Exception:
                pass
    finally:
        current = await store.get_account(account_id)
        if current and current.status == "running":
            await store.update_account(account_id, status="idle")
