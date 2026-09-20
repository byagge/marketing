from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Kind = Literal["telethon", "pyrogram"]


@dataclass
class PendingLogin:
    """In-memory login between phone → code → (optional 2FA)."""

    user_id: int
    kind: Kind
    session_name: str
    phone: str
    api_id: int
    api_hash: str
    out_dir: Path
    client: Any = None
    phone_code_hash: str = ""
    needs_password: bool = False
    session_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def stem_name(self) -> str:
        return f"{self.session_name}_{self.kind}"

    @property
    def expected_path(self) -> Path:
        return self.out_dir / f"{self.stem_name}.session"


_pending: dict[int, PendingLogin] = {}


def get_pending(user_id: int) -> PendingLogin | None:
    return _pending.get(user_id)


def set_pending(login: PendingLogin) -> None:
    _pending[login.user_id] = login


async def clear_pending(user_id: int) -> None:
    login = _pending.pop(user_id, None)
    if login is None:
        return
    await _disconnect(login)


async def _disconnect(login: PendingLogin) -> None:
    client = login.client
    if client is None:
        return
    try:
        if hasattr(client, "disconnect"):
            maybe = client.disconnect()
            if hasattr(maybe, "__await__"):
                await maybe
    except Exception:
        pass
    login.client = None
