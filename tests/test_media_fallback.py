"""Fallback: в чат нельзя фото → только текст (caption)."""

from types import SimpleNamespace

import pytest

from app.tg.scheduler import (
    _banned_rights_forbid_photos,
    is_media_forbidden_error,
    peer_allows_photos,
)


class _FakePhotosForbidden(Exception):
    """Имитация ChatSendPhotosForbiddenError без импорта telethon.errors."""


_FakePhotosForbidden.__name__ = "ChatSendPhotosForbiddenError"


class _FakeMediaForbidden(Exception):
    pass


_FakeMediaForbidden.__name__ = "ChatSendMediaForbiddenError"


def test_is_media_forbidden_detects_photos_error():
    assert is_media_forbidden_error(_FakePhotosForbidden("CHAT_SEND_PHOTOS_FORBIDDEN"))


def test_is_media_forbidden_detects_media_error():
    assert is_media_forbidden_error(_FakeMediaForbidden("CHAT_SEND_MEDIA_FORBIDDEN"))


def test_is_media_forbidden_detects_error_text():
    assert is_media_forbidden_error(
        RuntimeError("ChatSendPhotosForbiddenError: can't send photos")
    )
    assert is_media_forbidden_error(
        RuntimeError("ChatSendMediaForbiddenError: media forbidden")
    )


def test_is_media_forbidden_ignores_unrelated():
    assert not is_media_forbidden_error(RuntimeError("FloodWaitError: wait 30"))
    assert not is_media_forbidden_error(ValueError("Cannot find any entity"))


def test_banned_rights_forbid_photos():
    assert not _banned_rights_forbid_photos(None)
    assert not _banned_rights_forbid_photos(SimpleNamespace(send_photos=False, send_media=False))
    assert _banned_rights_forbid_photos(SimpleNamespace(send_photos=True, send_media=False))
    assert _banned_rights_forbid_photos(SimpleNamespace(send_photos=False, send_media=True))


@pytest.mark.asyncio
async def test_peer_allows_photos_admin():
    class Client:
        async def get_permissions(self, entity, user=None):
            return SimpleNamespace(
                is_admin=True,
                is_creator=False,
                has_default_permissions=False,
                participant=None,
            )

    assert await peer_allows_photos(Client(), object()) is True


@pytest.mark.asyncio
async def test_peer_allows_photos_default_banned():
    entity = SimpleNamespace(
        default_banned_rights=SimpleNamespace(send_photos=True, send_media=False)
    )

    class Client:
        async def get_permissions(self, ent, user=None):
            if user is None:
                return ent.default_banned_rights
            return SimpleNamespace(
                is_admin=False,
                is_creator=False,
                has_default_permissions=True,
                participant=SimpleNamespace(banned_rights=None),
            )

    assert await peer_allows_photos(Client(), entity) is False


@pytest.mark.asyncio
async def test_peer_allows_photos_personal_ban():
    class Client:
        async def get_permissions(self, entity, user=None):
            return SimpleNamespace(
                is_admin=False,
                is_creator=False,
                has_default_permissions=False,
                participant=SimpleNamespace(
                    banned_rights=SimpleNamespace(send_photos=True, send_media=False)
                ),
            )

    assert await peer_allows_photos(Client(), object()) is False
