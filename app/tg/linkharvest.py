"""Ссылка вступления «у соседа»: если один аккаунт уже в чате, берём у него @username/invite.

Так остальные аккаунты вступают сами, и человеку не нужно вручную искать ссылку для чата,
в котором в каталоге записан только числовой id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.tl import functions, types

log = logging.getLogger("marketing.harvest")

# в каталоге нет способа вступить (авто-пометка автопилота / вступления по id)
NO_LINK_MARKERS = ("нет способа", "нет ссылки")


def is_no_link_error(text: str) -> bool:
    low = (text or "").casefold()
    return any(m in low for m in NO_LINK_MARKERS)


@dataclass
class HarvestedLink:
    username: str = ""
    invite: str = ""

    @property
    def found(self) -> bool:
        return bool(self.username or self.invite)

    def describe(self) -> str:
        if self.username:
            return f"@{self.username}"
        if self.invite:
            return "invite-ссылку"
        return ""


def _public_username(entity) -> str:
    name = (getattr(entity, "username", None) or "").strip()
    if name:
        return name
    for u in getattr(entity, "usernames", None) or []:
        if getattr(u, "active", False) and getattr(u, "username", ""):
            return str(u.username)
    return ""


def _invite_link(exported) -> str:
    link = getattr(exported, "link", None)
    return str(link).strip() if link else ""


async def harvest_join_link(client: TelegramClient, entity) -> HarvestedLink:
    """Публичный @username или invite-ссылка чата по сущности аккаунта, который в чате.

    Никогда не бросает: на любой ошибке возвращает то, что успели найти.
    Сначала смотрим бесплатное (username, primary-ссылка из полной информации о чате);
    новую ссылку создаём только если прав хватает (иначе Telegram вернёт ошибку).
    """
    out = HarvestedLink(username=_public_username(entity))
    if out.username:
        return out
    try:
        if isinstance(entity, types.Channel):
            full = await client(functions.channels.GetFullChannelRequest(entity))
        else:
            full = await client(functions.messages.GetFullChatRequest(entity.id))
        out.invite = _invite_link(getattr(full.full_chat, "exported_invite", None))
    except Exception as e:  # noqa: BLE001
        log.info("harvest: полная информация недоступна: %s", type(e).__name__)
    if out.invite:
        return out
    try:
        res = await client(functions.messages.ExportChatInviteRequest(peer=entity))
        out.invite = _invite_link(res)
    except Exception as e:  # noqa: BLE001
        log.info("harvest: создать ссылку нельзя (%s)", type(e).__name__)
    return out
