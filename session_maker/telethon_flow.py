from __future__ import annotations

from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from session_maker.api import out_dir as default_out_dir
from session_maker.pending import PendingLogin


async def telethon_start_code(login: PendingLogin) -> str:
    """Connect + send code. Returns human status."""
    login.out_dir.mkdir(parents=True, exist_ok=True)
    stem = login.out_dir / login.stem_name
    client = TelegramClient(str(stem), login.api_id, login.api_hash)
    await client.connect()
    login.client = client

    if await client.is_user_authorized():
        me = await client.get_me()
        login.session_path = Path(str(stem) + ".session")
        await client.disconnect()
        login.client = None
        return (
            f"Уже авторизован: {me.first_name} (@{me.username or '-'}) "
            f"id={me.id}. Файл готов."
        )

    sent = await client.send_code_request(login.phone)
    login.phone_code_hash = sent.phone_code_hash
    return "Код отправлен в Telegram / SMS. Пришлите код."


async def telethon_submit_code(login: PendingLogin, code: str) -> str:
    """
    Returns:
      'ok' | 'password' | raises
    """
    client = login.client
    if client is None:
        raise RuntimeError("Клиент не активен — начните заново")
    try:
        await client.sign_in(
            phone=login.phone,
            code=code.strip(),
            phone_code_hash=login.phone_code_hash,
        )
    except SessionPasswordNeededError:
        login.needs_password = True
        return "password"
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        raise
    return await _telethon_finish(login)


async def telethon_submit_password(login: PendingLogin, password: str) -> str:
    client = login.client
    if client is None:
        raise RuntimeError("Клиент не активен — начните заново")
    await client.sign_in(password=password)
    return await _telethon_finish(login)


async def _telethon_finish(login: PendingLogin) -> str:
    client = login.client
    assert client is not None
    me = await client.get_me()
    path = login.expected_path
    await client.disconnect()
    login.client = None
    login.session_path = path
    if not path.exists():
        raise RuntimeError(f"Файл session не найден: {path}")
    return (
        f"Telethon готов: {me.first_name} (@{me.username or '-'}) id={me.id}"
        f"{' | Premium' if getattr(me, 'premium', False) else ''}"
    )


def ensure_out(login: PendingLogin) -> Path:
    login.out_dir = login.out_dir or default_out_dir()
    login.out_dir.mkdir(parents=True, exist_ok=True)
    return login.out_dir
