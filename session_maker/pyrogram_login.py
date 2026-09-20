from __future__ import annotations

import asyncio
from pathlib import Path


def create_pyrogram_session(
    *,
    out_dir: Path,
    session_name: str,
    phone: str,
    api_id: int,
    api_hash: str,
) -> Path:
    try:
        from pyrogram import Client
    except ImportError as e:
        raise SystemExit(
            "Pyrogram не установлен. В корне marketing:\n"
            "  pip install pyrogram tgcrypto"
        ) from e

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Pyrogram writes {workdir}/{name}.session
    name = f"{session_name}_pyrogram"
    session_file = out_dir / f"{name}.session"

    async def _run() -> None:
        client = Client(
            name=name,
            api_id=api_id,
            api_hash=api_hash,
            phone_number=phone,
            workdir=str(out_dir),
            in_memory=False,
        )
        print("  Pyrogram: код придёт в Telegram / SMS…")
        await client.start()
        try:
            me = await client.get_me()
            print(
                f"  Вошли: {me.first_name} (@{me.username or '-'}) id={me.id}"
                f"{' | Premium' if getattr(me, 'is_premium', False) else ''}"
            )
        finally:
            await client.stop()

    asyncio.run(_run())
    if not session_file.exists():
        raise RuntimeError(f"Pyrogram session не создан: {session_file}")
    return session_file
