from __future__ import annotations

from pathlib import Path

from session_maker.pending import PendingLogin


async def pyrogram_start_code(login: PendingLogin) -> str:
    try:
        from pyrogram import Client
    except ImportError as e:
        raise RuntimeError("Pyrogram не установлен (pip install pyrogram tgcrypto)") from e

    login.out_dir.mkdir(parents=True, exist_ok=True)
    client = Client(
        name=login.stem_name,
        api_id=login.api_id,
        api_hash=login.api_hash,
        workdir=str(login.out_dir),
        in_memory=False,
        phone_number=login.phone,
    )
    await client.connect()
    login.client = client

    user_id = await client.storage.user_id()
    if user_id:
        me = await client.get_me()
        login.session_path = login.expected_path
        await client.disconnect()
        login.client = None
        return (
            f"Уже авторизован: {me.first_name} (@{me.username or '-'}) "
            f"id={me.id}. Файл готов."
        )

    sent = await client.send_code(login.phone)
    login.phone_code_hash = sent.phone_code_hash
    return "Код отправлен в Telegram / SMS. Пришлите код."


async def pyrogram_submit_code(login: PendingLogin, code: str) -> str:
    from pyrogram.errors import (
        PhoneCodeExpired,
        PhoneCodeInvalid,
        SessionPasswordNeeded,
    )

    client = login.client
    if client is None:
        raise RuntimeError("Клиент не активен — начните заново")
    try:
        await client.sign_in(
            phone_number=login.phone,
            phone_code_hash=login.phone_code_hash,
            phone_code=code.strip(),
        )
    except SessionPasswordNeeded:
        login.needs_password = True
        return "password"
    except (PhoneCodeInvalid, PhoneCodeExpired):
        raise
    return await _pyrogram_finish(login)


async def pyrogram_submit_password(login: PendingLogin, password: str) -> str:
    client = login.client
    if client is None:
        raise RuntimeError("Клиент не активен — начните заново")
    await client.check_password(password)
    return await _pyrogram_finish(login)


async def _pyrogram_finish(login: PendingLogin) -> str:
    client = login.client
    assert client is not None
    me = await client.get_me()
    path = login.expected_path
    await client.disconnect()
    login.client = None
    login.session_path = path
    if not path.exists():
        # Pyrogram sometimes keeps journal; wait/stat
        raise RuntimeError(f"Файл session не найден: {path}")
    return (
        f"Pyrogram готов: {me.first_name} (@{me.username or '-'}) id={me.id}"
        f"{' | Premium' if getattr(me, 'is_premium', False) else ''}"
    )
