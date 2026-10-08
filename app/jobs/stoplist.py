"""Стоп-лист: чаты, в которые писать нельзя («Отзывы» и т. п.) — отключаем отправку везде."""

from __future__ import annotations

from app.config import get_settings
from app.models import Chat
from app.sender_api import SenderAPI, SenderAPIError
from app.store import Store
from app.utils.chat_ids import chat_ids_match
from app.utils.send_policy import is_no_post, matches_stoplist


def _catalog_match(cid: str, chats: list[Chat]) -> Chat | None:
    for chat in chats:
        if chat_ids_match(cid, chat.chat_id):
            return chat
    return None


async def sweep_stoplist(store: Store, account, api: SenderAPI | None = None) -> list[str]:
    """
    Выключить в Autoposter аккаунта все живые чаты из стоп-листа / с флагом «писать нельзя».
    Ловит и те, что не в каталоге (sender шлёт во все диалоги аккаунта).
    Возвращает названия чатов, которые выключили.
    """
    sid = (account.sender_account_id or "").strip()
    if not sid:
        return []
    keywords = await store.get_stoplist()
    catalog = await store.list_chats()
    if not keywords and not any(c.no_post for c in catalog):
        return []
    cfg = get_settings()
    api = api or SenderAPI(cfg.sender_api_url, cfg.sender_api_key)
    try:
        live = await api.list_chats(sid)
    except SenderAPIError:
        return []
    done: list[str] = []
    for item in live:
        cid = str(item.get("chat_id") or "")
        if not cid or item.get("active") is False:
            continue
        title = str(item.get("title") or "")
        cat = _catalog_match(cid, catalog)
        if not (matches_stoplist(title, keywords) or (cat and is_no_post(cat, keywords))):
            continue
        try:
            await api.patch_chat(sid, cid, active=False)
        except SenderAPIError:
            continue
        done.append(title or (cat.display_name if cat else cid))
    return done


async def apply_stoplist_everywhere(store: Store, bot=None, admin_chat_id=None) -> str:
    """
    Применить стоп-лист ко всем аккаунтам: каталожные чаты (schedule и sender) —
    через apply_chat_all, остальные живые чаты sender — прямой зачисткой в Autoposter.
    """
    from app.jobs.parallel import map_batches, setup_parallel_defaults
    from app.jobs.prefs import apply_chat_all

    keywords = await store.get_stoplist()
    catalog = [c for c in await store.list_chats() if is_no_post(c, keywords)]
    for chat in catalog:
        await apply_chat_all(store, chat.id, bot, admin_chat_id)
    accounts = [
        a
        for a in await store.list_accounts()
        if (a.sender_account_id or "").strip() and a.sender_on and not a.sender_forbidden
    ]
    size, pause = setup_parallel_defaults()

    async def _one(acc):
        return await sweep_stoplist(store, acc)

    swept = 0
    for r in await map_batches(accounts, _one, batch_size=size, batch_pause=pause):
        if not isinstance(r, BaseException):
            swept += len(r)
    return (
        f"Стоп-лист применён: каталожных чатов {len(catalog)}, "
        f"живых чатов выключено в Autoposter {swept}"
    )
