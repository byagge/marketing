from __future__ import annotations

from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    PeerIdInvalidError,
    UserBannedInChannelError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

# Errors that mean "account is not in this chat / cannot resolve it yet".
# Setup should skip these and retry later instead of aborting the whole job.
_UNAVAILABLE_TYPES = (
    ChannelPrivateError,
    ChannelInvalidError,
    ChatWriteForbiddenError,
    ChatAdminRequiredError,
    UserBannedInChannelError,
    PeerIdInvalidError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    ValueError,
    TypeError,
)

_UNAVAILABLE_MARKERS = (
    "cannot find any entity",
    "no user has",
    "could not find the input entity",
    "nobody is using this username",
    "the key is not registered",
    "peer id invalid",
    "chat not found",
    "channel not found",
    "чат не найден",
    "you're banned from sending",
    "banned from sending messages",
)

_UNAVAILABLE_NAME_MARKERS = (
    "userbannedinchannel",
    "channelprivate",
    "channelinvalid",
    "chatwriteforbidden",
    "chatadminrequired",
    "peeridinvalid",
    "usernameinvalid",
    "usernamenotoccupied",
    "invitehashexpired",
    "invitehashinvalid",
)


def is_unavailable_text(text: str) -> bool:
    """Распознать unavailable по тексту ошибки (в т.ч. из result['error'])."""
    msg = (text or "").casefold()
    if not msg:
        return False
    if any(marker in msg for marker in _UNAVAILABLE_MARKERS):
        return True
    compact = msg.replace(" ", "").replace("_", "")
    head = compact.split(":", 1)[0]
    return any(n in head or n in compact for n in _UNAVAILABLE_NAME_MARKERS)


def is_chat_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, _UNAVAILABLE_TYPES):
        return True
    return is_unavailable_text(str(exc))


def format_unavailable(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"
