"""Чаты аккаунта: список, вкл/выкл отправки, отметки, свой текст для чата."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.models import Account, Chat, ChatPref, Restriction, SetupState
from app.sender_api import SenderAPI, SenderAPIError
from app.store import Store
from app.tg.client import telethon_client
from app.tg.resolve import lookup_entity
from app.tg.scheduler import delete_scheduled
from app.utils.chat_ids import canon_chat_id
from app.utils.entities import entities_loads
from app.utils.templates import render_post

log = logging.getLogger("marketing.chatprefs")

_live_cache: dict[int, tuple[float, list[dict]]] = {}
LIVE_TTL = 90.0


@dataclass
class ChatItem:
    key: str
    chat_id: str
    title: str
    kind: str  # schedule | sender | live
    username: str = ""
    invite: str = ""
    chat: Chat | None = None
    pref: ChatPref | None = None
    restriction: Restriction | None = None
    state: SetupState | None = None
    has_text: bool = False
    globally_off: bool = False

    @property
    def enabled(self) -> bool:
        return self.pref is None or self.pref.is_enabled

    @property
    def link(self) -> str:
        if self.username:
            return f"https://t.me/{self.username.lstrip('@')}"
        if self.invite:
            return self.invite
        if self.key.isdigit():
            return f"https://t.me/c/{self.key}/1"
        return ""

    @property
    def mention_label(self) -> str:
        if self.kind == "schedule":
            return "—"
        m = -1 if self.pref is None else self.pref.mention
        return {-1: "общая", 0: "выкл", 1: "вкл"}.get(m, "общая")


def _sender_id(acc: Account) -> str:
    return (acc.sender_account_id or "").strip()


async def fetch_live_chats(acc: Account, *, force: bool = False) -> list[dict]:
    sid = _sender_id(acc)
    if not sid:
        return []
    import time

    cached = _live_cache.get(acc.id)
    if cached and not force and time.monotonic() - cached[0] < LIVE_TTL:
        return cached[1]
    s = get_settings()
    api = SenderAPI(s.sender_api_url, s.sender_api_key, timeout=15.0)
    try:
        chats = await asyncio.wait_for(api.list_chats(sid), 20.0)
    except Exception as e:  # noqa: BLE001
        log.info("live chats unavailable for %s: %s", acc.label, e)
        return cached[1] if cached else []
    _live_cache[acc.id] = (time.monotonic(), chats)
    return chats


async def list_account_items(
    store: Store, acc: Account, *, include_live: bool = True
) -> list[ChatItem]:
    chats = await store.list_chats()
    prefs = {p.chat_key: p for p in await store.list_prefs(acc.id)}
    restr = {r.chat_pk: r for r in await store.list_restrictions(account_id=acc.id)}
    states = {s.chat_pk: s for s in await store.list_setup_states(acc.id)}
    texts = await store.chat_post_keys(acc.id)

    items: list[ChatItem] = []
    seen: set[str] = set()
    for chat in chats:
        key = canon_chat_id(chat.chat_id)
        seen.add(key)
        items.append(
            ChatItem(
                key=key,
                chat_id=chat.chat_id,
                title=chat.display_name,
                kind="schedule" if chat.is_schedule else "sender",
                username=chat.username or "",
                invite=chat.invite_link or "",
                chat=chat,
                pref=prefs.get(key),
                restriction=restr.get(chat.id),
                state=states.get(chat.id),
                has_text=key in texts,
                globally_off=not chat.enabled,
            )
        )
    if include_live:
        for live in await fetch_live_chats(acc):
            cid = str(live.get("chat_id") or "")
            key = canon_chat_id(cid)
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(
                ChatItem(
                    key=key,
                    chat_id=cid,
                    title=str(live.get("title") or cid),
                    kind="live",
                    username=str(live.get("username") or ""),
                    pref=prefs.get(key),
                    has_text=key in texts,
                )
            )
    items.sort(key=lambda i: ({"schedule": 0, "sender": 1, "live": 2}[i.kind], i.title.casefold()))
    return items


async def find_item(store: Store, acc: Account, key: str) -> ChatItem | None:
    for item in await list_account_items(store, acc):
        if item.key == key:
            return item
    return None


async def set_enabled(store: Store, acc: Account, item: ChatItem, enabled: bool) -> str:
    """Вкл/выкл отправку этого аккаунта в чат. Возвращает короткий итог."""
    await store.set_pref(acc.id, item.chat_id, enabled=enabled)
    notes: list[str] = []
    if item.chat is not None and item.chat.is_schedule:
        chat = item.chat
        if not enabled:
            if acc.telethon_session:
                try:
                    async with telethon_client(acc.telethon_session) as client:
                        entity = await lookup_entity(client, chat)
                        if entity is not None:
                            n = await delete_scheduled(client, entity)
                            notes.append(f"удалено отложенных: {n}")
                except Exception as e:  # noqa: BLE001
                    notes.append(f"отложенные не удалены ({type(e).__name__})")
            await store.delete_slot(chat.id, acc.id)
        else:
            await store.reset_setup_state(acc.id, chat.id)
            notes.append("будет настроено при ближайшем запуске")
    else:
        sid = _sender_id(acc)
        if sid:
            s = get_settings()
            api = SenderAPI(s.sender_api_url, s.sender_api_key, timeout=30.0)
            try:
                await api.patch_chat(sid, item.chat_id, active=bool(enabled))
                notes.append("Autoposter обновлён")
            except SenderAPIError as e:
                notes.append(f"Autoposter: {e}")
    return "; ".join(notes)


async def cycle_mention(store: Store, acc: Account, item: ChatItem) -> str:
    """общая → вкл → выкл → общая (только sender-чаты)."""
    if item.kind == "schedule":
        return "Отметки работают только в sender-чатах (Autoposter)"
    cur = -1 if item.pref is None else item.pref.mention
    nxt = {-1: 1, 1: 0, 0: -1}.get(cur, -1)
    await store.set_pref(acc.id, item.chat_id, mention=nxt)
    sid = _sender_id(acc)
    if not sid:
        return f"Сохранено ({ {-1:'общая',0:'выкл',1:'вкл'}[nxt] }); Sender ID не задан"
    s = get_settings()
    api = SenderAPI(s.sender_api_url, s.sender_api_key, timeout=30.0)
    ss = await store.sender_settings()
    value: Any = True if nxt == 1 else False if nxt == 0 else ("global" if ss.mentions_enabled else False)
    try:
        await api.patch_chat(sid, item.chat_id, mention=value)
    except SenderAPIError as e:
        return f"Сохранено, но Autoposter: {e}"
    return {-1: "отметки: как в общих настройках", 0: "отметки выкл", 1: "отметки вкл"}[nxt]


async def apply_custom_text(store: Store, acc: Account, item: ChatItem, bot=None, admin_chat_id=None) -> str:
    """Применить свой текст чата сразу (schedule — пересоздать отложенные, sender — PATCH)."""
    if item.chat is not None and item.chat.is_schedule:
        from app.jobs.setup import run_setup_chats_only

        buckets = await run_setup_chats_only(
            store,
            acc.id,
            [item.chat.id],
            bot,
            admin_chat_id,
            job_kind="setup_retry",
            skip_abandoned=False,
        )
        ok = bool(buckets.get("ok"))
        return "schedule пересоздан" if ok else "текст сохранён, schedule не обновился (см. отчёт)"
    sid = _sender_id(acc)
    if not sid:
        return "текст сохранён (Sender ID не задан)"
    from app.tg.sender_push import push_chat_text

    post = await store.get_chat_post(acc.id, item.chat_id)
    if post is None:
        return "текст сброшен — применится общий при следующем запуске"
    tag = item.chat.tag if item.chat else None
    text, ents = render_post(post.text, entities_loads(post.entities_json), tag)
    s = get_settings()
    api = SenderAPI(s.sender_api_url, s.sender_api_key, timeout=30.0)
    pref = item.pref
    ss = await store.sender_settings()
    mentions = ss.mentions_enabled if pref is None or pref.mention < 0 else bool(pref.mention)
    try:
        await push_chat_text(api, sid, item.chat_id, text, ents, mentions_enabled=mentions)
    except SenderAPIError as e:
        return f"Autoposter: {e}"
    return "текст отправлен в Autoposter"
