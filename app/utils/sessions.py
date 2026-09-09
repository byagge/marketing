from __future__ import annotations

import sqlite3
from pathlib import Path


def detect_session_kind(path: str | Path) -> str:
    path = Path(path)
    if not path.exists() or path.stat().st_size < 100:
        return "unknown"
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            con.close()
    except sqlite3.Error:
        return "unknown"

    if "entities" in tables:
        return "telethon"
    if "peers" in tables or "usernames" in tables:
        return "pyrogram"
    return "unknown"
