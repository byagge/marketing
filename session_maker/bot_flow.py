from __future__ import annotations

"""
Bot-facing helpers for stepwise Telethon/Pyrogram session login.
CLI remains in session_maker.cli — this module is for the Telegram bot FSM.
"""

from pathlib import Path

from session_maker.api import load_api, normalize_phone, out_dir, sanitize_session_name
from session_maker.pending import PendingLogin, clear_pending, get_pending, set_pending
from session_maker.pyrogram_flow import (
    pyrogram_start_code,
    pyrogram_submit_code,
    pyrogram_submit_password,
)
from session_maker.telethon_flow import (
    telethon_start_code,
    telethon_submit_code,
    telethon_submit_password,
)


async def begin_login(
    *,
    user_id: int,
    kind: str,
    session_name: str,
    phone: str,
) -> tuple[PendingLogin, str]:
    """Create pending login and send SMS/Telegram code. Returns (login, status_text)."""
    await clear_pending(user_id)
    if kind not in {"telethon", "pyrogram"}:
        raise ValueError("kind: telethon | pyrogram")
    api_id, api_hash = load_api()
    login = PendingLogin(
        user_id=user_id,
        kind=kind,  # type: ignore[arg-type]
        session_name=sanitize_session_name(session_name),
        phone=normalize_phone(phone),
        api_id=api_id,
        api_hash=api_hash,
        out_dir=out_dir(),
    )
    set_pending(login)
    if kind == "telethon":
        status = await telethon_start_code(login)
    else:
        status = await pyrogram_start_code(login)
    # Already authorized → file ready, drop pending.
    if login.client is None and login.session_path is not None:
        await clear_pending(user_id)
    return login, status


async def submit_code(user_id: int, code: str) -> tuple[str, Path | None]:
    """
    Returns (status, path_or_none).
    status: 'ok' | 'password' | error text via exception
    """
    login = get_pending(user_id)
    if login is None:
        raise RuntimeError("Нет активного входа — начните заново")
    if login.kind == "telethon":
        result = await telethon_submit_code(login, code)
    else:
        result = await pyrogram_submit_code(login, code)
    if result == "password":
        return "password", None
    path = login.session_path
    note = result
    await clear_pending(user_id)
    return note, path


async def submit_password(user_id: int, password: str) -> tuple[str, Path]:
    login = get_pending(user_id)
    if login is None:
        raise RuntimeError("Нет активного входа — начните заново")
    if login.kind == "telethon":
        note = await telethon_submit_password(login, password)
    else:
        note = await pyrogram_submit_password(login, password)
    path = login.session_path
    await clear_pending(user_id)
    if path is None or not path.exists():
        raise RuntimeError("Session-файл не создан")
    return note, path
