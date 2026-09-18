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
)


def is_chat_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, _UNAVAILABLE_TYPES):
        return True
    msg = str(exc).casefold()
    return any(marker in msg for marker in _UNAVAILABLE_MARKERS)


def format_unavailable(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"
