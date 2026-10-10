"""Быстрое применение настроек пары аккаунт × чат и аккаунта-sender без полной настройки."""

from __future__ import annotations

from aiogram import Bot

from app.config import get_settings
from app.jobs import LogSink
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.jobs.setup import (
    clear_scheduled_for_pair,
    run_setup_chats_only,
)
from app.jobs.spam import account_load, sync_sender_load
from app.models import Account, Chat
from app.sender_api import SenderAPI, SenderAPIError
from app.store import Store
from app.tg.client import telethon_client
from app.tg.sender_push import push_chat_text
from app.utils.chat_ids import chat_ids_match
from app.utils.entities import entities_loads
from app.utils.send_policy import (
    REASON_LABEL,
    effective_interval_seconds,
    is_send_allowed,
    pick_text_override,
)
from app.utils.spamstate import scale_seconds
from app.utils.templates import pick_post_for_chat, render_post
from app.utils.errfmt import short_error


def _api() -> SenderAPI:
    s = get_settings()
    return SenderAPI(s.sender_api_url, s.sender_api_key)


async def _pair_text(store: Store, account: Account, chat: Chat, pref) -> tuple[str, list]:
    override = pick_text_override(pref)
    if override is not None:
        return render_post(override, entities_loads(pref.entities_json), chat.tag)
    posts = {
        lang: await store.get_post(account.id, lang)
        for lang in ("ru", "en", "ru_short", "en_short")
    }
    post = pick_post_for_chat(posts, chat)
    return render_post(post.text, entities_loads(post.entities_json), chat.tag)


async def apply_pair(
    store: Store,
    account_id: int,
    chat_pk: int,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
) -> str:
    """Применить выключатель/текст пары сразу: sender — через API, schedule — через Telethon."""
    account = await store.get_account(account_id)
    chat = await store.get_chat(chat_pk)
    if not account or not chat:
        return "аккаунт или чат не найден"
    pref = await store.get_chat_pref(account.id, chat.id)
    ban = await store.get_ban(account.id, chat.id)
    load = await account_load(store, account)
    allowed, why = is_send_allowed(
        account,
        chat,
        pref,
        bool(ban and ban.active),
        spam_load=load,
        stoplist=await store.get_stoplist(),
    )
    job = await store.create_job("pair_apply", account.id)
    log = LogSink(store, job.id, bot, admin_chat_id)
    try:
        if chat.is_schedule:
            result = await _apply_schedule(store, account, chat, allowed, why, bot, admin_chat_id, log)
        else:
            result = await _apply_sender(store, account, chat, pref, allowed, why, load)
        await store.finish_job(job.id, "done", result)
        return result
    except Exception as e:
        err = f"{short_error(e)}"
        await store.finish_job(job.id, "error", err)
        return f"ошибка: {err}"


async def _apply_schedule(
    store: Store,
    account: Account,
    chat: Chat,
    allowed: bool,
    why: str,
    bot: Bot | None,
    admin_chat_id: int | None,
    log: LogSink,
) -> str:
    if not account.telethon_session:
        return "нет Telethon session — schedule не применить"
    if not allowed:
        async with telethon_client(account.telethon_session) as client:
            n = await clear_scheduled_for_pair(client, chat, account, why, log)
        return f"{REASON_LABEL.get(why, why)}: убрал запланированных — {n}"
    buckets = await run_setup_chats_only(
        store,
        account.id,
        [chat.id],
        bot,
        admin_chat_id,
        job_kind="pair_apply_schedule",
        skip_abandoned=False,
    )
    if buckets.get("ok"):
        return "schedule перепланирован"
    bad = (
        buckets.get("skipped") or buckets.get("config_error") or buckets.get("abandoned") or []
    )
    return "schedule не настроен: " + (", ".join(bad) or "см. логи")


async def _apply_sender(
    store: Store,
    account: Account,
    chat: Chat,
    pref,
    allowed: bool,
    why: str,
    load: int = 100,
) -> str:
    sid = (account.sender_account_id or "").strip()
    if not sid:
        return "у аккаунта нет Sender ID — настроится при «Настройка»"
    api = _api()
    live = await api.list_chats(sid)
    live_chat = next(
        (c for c in live if chat_ids_match(c.get("chat_id"), chat.chat_id)), None
    )
    if live_chat is None:
        return "чата нет среди диалогов аккаунта в Autoposter (аккаунт не в чате?)"
    cid = str(live_chat["chat_id"])
    if not allowed:
        await api.patch_chat(sid, cid, active=False)
        return f"выключил в Autoposter: {REASON_LABEL.get(why, why)}"
    text, ents = await _pair_text(store, account, chat, pref)
    if not text.strip():
        return "нет текста для этого чата — задайте пост или свой текст"
    ss = await store.sender_settings()
    await push_chat_text(api, sid, cid, text, ents, mentions_enabled=ss.mentions_enabled)
    if int(chat.max_posts_per_account or 0) > 0:
        await api.patch_chat(
            sid, cid, interval=scale_seconds(effective_interval_seconds(chat), load)
        )
    try:
        info = await api.get_account(sid)
        if not info.get("spam"):
            await api.spam_start(sid)
    except SenderAPIError as e:
        return f"текст обновлён, но рассылка не запущена: {e}"
    return "включил и обновил текст в Autoposter"


async def apply_account_sender(
    store: Store,
    account_id: int,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
) -> str:
    """Применить выключатель sender / dead / нагрузку аккаунта в Autoposter."""
    account = await store.get_account(account_id)
    if not account:
        return "аккаунт не найден"
    if account.sender_on and not account.sender_forbidden and not account.has_sender:
        return "sender включён, но у аккаунта нет Sender ID / Pyrogram+token"
    return await sync_sender_load(store, account_id, bot, admin_chat_id)


async def apply_chat_all(
    store: Store,
    chat_pk: int,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
) -> str:
    """Применить настройки чата (например «писать нельзя») ко всем аккаунтам."""
    chat = await store.get_chat(chat_pk)
    if not chat:
        return "чат не найден"
    accounts = [
        a
        for a in await store.list_accounts()
        if a.telethon_session or (a.sender_account_id or "").strip()
    ]
    size, pause = setup_parallel_defaults()

    async def _one(acc: Account) -> str:
        return await apply_pair(store, acc.id, chat.id)

    results = await map_batches(accounts, _one, batch_size=size, batch_pause=pause)
    bad = sum(1 for r in results if isinstance(r, BaseException) or str(r).startswith("ошибка"))
    return f"«{chat.display_name}»: применено к {len(results) - bad} из {len(results)} аккаунтам"


async def apply_chat_limit(
    store: Store,
    chat_pk: int,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
) -> str:
    """Применить лимит «постов на аккаунт в сутки» для чата ко всем аккаунтам."""
    chat = await store.get_chat(chat_pk)
    if not chat:
        return "чат не найден"
    accounts = await store.list_accounts()
    prefs = await store.prefs_for_chat(chat.id)
    bans = {b.account_id for b in await store.list_bans() if b.chat_pk == chat.id}
    done = failed = 0

    if chat.is_schedule:
        slots = await store.slots_for_chat(chat.id)
        ids = [
            s.account_id
            for s in slots
            if (prefs.get(s.account_id) is None or prefs[s.account_id].enabled)
            and s.account_id not in bans
        ]
        size, pause = setup_parallel_defaults()

        async def _one(aid: int):
            return await run_setup_chats_only(
                store, aid, [chat.id], bot, admin_chat_id,
                job_kind="chat_limit", skip_abandoned=False,
            )

        results = await map_batches(ids, _one, batch_size=size, batch_pause=pause)
        for r in results:
            if isinstance(r, BaseException) or not r.get("ok"):
                failed += 1
            else:
                done += 1
    else:
        api = _api()
        for acc in accounts:
            sid = (acc.sender_account_id or "").strip()
            if not sid or not acc.sender_on:
                continue
            pref = prefs.get(acc.id)
            if (pref is not None and not pref.enabled) or acc.id in bans:
                continue
            try:
                live = await api.list_chats(sid)
                live_chat = next(
                    (c for c in live if chat_ids_match(c.get("chat_id"), chat.chat_id)), None
                )
                if live_chat is None:
                    continue
                cid = str(live_chat["chat_id"])
                if int(chat.max_posts_per_account or 0) > 0:
                    await api.patch_chat(sid, cid, interval=effective_interval_seconds(chat))
                else:
                    await api.patch_chat(sid, cid, clear_interval=True)
                done += 1
            except SenderAPIError:
                failed += 1
    limit = int(chat.max_posts_per_account or 0)
    label = f"{limit} постов/аккаунт/сутки" if limit else "без лимита"
    return f"лимит чата «{chat.display_name}»: {label} — применено {done}, ошибок {failed}"
