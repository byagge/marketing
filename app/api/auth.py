from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Header, HTTPException

from app.config import DATA_DIR, get_settings

KEY_FILE = DATA_DIR / "api_key.txt"


def resolve_api_key() -> str:
    settings = get_settings()
    key = (settings.api_key or "").strip()
    if key:
        return key
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        existing = KEY_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    key = secrets.token_urlsafe(32)
    KEY_FILE.write_text(key + "\n", encoding="utf-8")
    return key


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    expected = resolve_api_key()
    got = (x_api_key or "").strip()
    if not got and authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        else:
            got = auth
    if not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="invalid api key")
