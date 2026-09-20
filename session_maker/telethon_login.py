from __future__ import annotations

import asyncio
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


def create_telethon_session(
    *,
    out_dir: Path,
    session_name: str,
    phone: str,
    api_id: int,
    api_hash: str,
) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Telethon adds .session itself to the stem path.
    stem = out_dir / f"{session_name}_telethon"
    session_file = Path(str(stem) + ".session")

    async def _run() -> None:
        client = TelegramClient(str(stem), api_id, api_hash)
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                print(
                    f"  Уже авторизован: {me.first_name} "
                    f"(@{me.username or '-'}) id={me.id}"
                )
                return

            print("  Отправляю код в Telegram…")
            await client.send_code_request(phone)
            code = input("  Код из Telegram: ").strip()
            try:
                await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                password = input("  Облачный пароль (2FA): ").strip()
                await client.sign_in(password=password)

            me = await client.get_me()
            print(
                f"  Вошли: {me.first_name} (@{me.username or '-'}) id={me.id}"
                f"{' | Premium' if getattr(me, 'premium', False) else ''}"
            )
        finally:
            await client.disconnect()

    asyncio.run(_run())
    if not session_file.exists():
        raise RuntimeError(f"Telethon session не создан: {session_file}")
    return session_file
