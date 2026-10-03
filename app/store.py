from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from shutil import copy2
from typing import Any

import aiosqlite

from app.config import DB_PATH, POSTS_DIR, ensure_dirs
from app.models import (
    Account,
    Chat,
    ChatPref,
    Job,
    JobLog,
    MinuteSlot,
    OnlinePingSettings,
    Post,
    Restriction,
    SenderSettings,
    SetupState,
    DailyReport,
    HealthSnapshot,
    Incident,
    Lead,
    LeadStatDay,
    SendStatDay,
)
from app.utils.chat_ids import canon_chat_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    telethon_session TEXT NOT NULL DEFAULT '',
    pyrogram_session TEXT NOT NULL DEFAULT '',
    sender_bot_token TEXT NOT NULL DEFAULT '',
    sender_account_id TEXT NOT NULL DEFAULT '',
    is_premium INTEGER NOT NULL DEFAULT 0,
    online_ping INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'idle',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    account_id INTEGER NOT NULL,
    lang TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    entities_json TEXT NOT NULL DEFAULT '[]',
    photo_path TEXT NOT NULL DEFAULT '',
    delivery TEXT NOT NULL DEFAULT 'post',
    link_url TEXT NOT NULL DEFAULT '',
    link_chat_id TEXT NOT NULL DEFAULT '',
    link_msg_id INTEGER NOT NULL DEFAULT 0,
    link_photo_url TEXT NOT NULL DEFAULT '',
    link_photo_chat_id TEXT NOT NULL DEFAULT '',
    link_photo_msg_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, lang)
);

CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    chat_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'sender',
    lang TEXT NOT NULL DEFAULT 'ru',
    tag TEXT NOT NULL DEFAULT '',
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    invite_link TEXT NOT NULL DEFAULT '',
    captcha_kind TEXT NOT NULL DEFAULT 'auto',
    join_mode TEXT NOT NULL DEFAULT 'direct',
    garant_bot TEXT NOT NULL DEFAULT '',
    after_join TEXT NOT NULL DEFAULT 'none',
    after_join_bot TEXT NOT NULL DEFAULT '',
    require_channels TEXT NOT NULL DEFAULT '',
    is_join_request INTEGER NOT NULL DEFAULT 0,
    text_kind TEXT NOT NULL DEFAULT 'full',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS minute_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_pk INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    start_minute INTEGER NOT NULL,
    UNIQUE(chat_pk, account_id),
    UNIQUE(chat_pk, start_minute)
);

CREATE TABLE IF NOT EXISTS setup_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    chat_pk INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    UNIQUE(account_id, chat_pk)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    report TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    chat_pk INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    UNIQUE(account_id, chat_pk, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_send_events_time ON send_events(sent_at);

CREATE TABLE IF NOT EXISTS send_scans (
    account_id INTEGER NOT NULL,
    chat_pk INTEGER NOT NULL,
    scanned_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, chat_pk)
);

CREATE TABLE IF NOT EXISTS chat_restrictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    chat_pk INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    reason_link TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    detected_at TEXT NOT NULL,
    until_at TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    resolved_at TEXT NOT NULL DEFAULT '',
    UNIQUE(account_id, chat_pk)
);

CREATE TABLE IF NOT EXISTS account_chat_prefs (
    account_id INTEGER NOT NULL,
    chat_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    mention INTEGER NOT NULL DEFAULT -1,
    PRIMARY KEY (account_id, chat_key)
);

CREATE TABLE IF NOT EXISTS account_chat_posts (
    account_id INTEGER NOT NULL,
    chat_key TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    entities_json TEXT NOT NULL DEFAULT '[]',
    photo_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, chat_key)
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    chat_pk INTEGER REFERENCES chats(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    source TEXT NOT NULL DEFAULT 'marketer',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    chat_pk INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    expected INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    sent_est REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS send_stats_daily (
    day TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    chat_pk INTEGER,
    messages REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, account_id, channel, chat_pk)
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    msg_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_id, user_id)
);

CREATE TABLE IF NOT EXISTS lead_stats_daily (
    day TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    new_leads INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, account_id)
);

CREATE TABLE IF NOT EXISTS density_daily (
    day TEXT NOT NULL,
    chat_pk INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    accounts_live INTEGER NOT NULL DEFAULT 0,
    avg_gap_min REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'empty',
    advice TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (day, chat_pk)
);

CREATE TABLE IF NOT EXISTS daily_reports (
    day TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dm_people (
    account_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    started_by TEXT NOT NULL DEFAULT 'unknown',
    first_at TEXT NOT NULL DEFAULT '',
    last_in_at TEXT NOT NULL DEFAULT '',
    last_msg_id INTEGER NOT NULL DEFAULT 0,
    last_msg_at TEXT NOT NULL DEFAULT '',
    in_count INTEGER NOT NULL DEFAULT 0,
    unread INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, user_id)
);

CREATE TABLE IF NOT EXISTS dm_activity (
    account_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    PRIMARY KEY (account_id, user_id, day)
);

CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status, severity);
CREATE INDEX IF NOT EXISTS idx_health_snap_acc ON health_snapshots(account_id, chat_pk, checked_at);
CREATE INDEX IF NOT EXISTS idx_leads_acc ON leads(account_id, last_seen_at);
"""

DEFAULT_SETTINGS = {
    "between_min": "30",
    "between_max": "90",
    "cycle_min": "300",
    "cycle_max": "600",
    "per_chat_min": "3600",
    "per_chat_max": "3600",
    "parallel": "4",
    "cloak_enabled": "0",
    "cloak_text": "",
    "cloak_entities_json": "[]",
    "mentions_enabled": "0",
    "keep_extra_ids": "",
    "online_ping_enabled": "1",
    "online_ping_hours": "3.5",
    "online_ping_jitter_sec": "1800",
    "online_hold_seconds": "4",
    "online_ping_last_at": "",
    "online_ping_next_at": "",
    "marketer_enabled": "1",
    "marketer_last_run_at": "",
    "marketer_last_digest_at": "",
    "marketer_auto_fix": "1",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _post(row: aiosqlite.Row | None, *, account_id: int, lang: str) -> Post:
    if not row:
        return Post(lang=lang, account_id=account_id)
    keys = row.keys()

    def _s(name: str, default: str = "") -> str:
        return (row[name] if name in keys else default) or default

    def _i(name: str, default: int = 0) -> int:
        if name not in keys or row[name] is None:
            return default
        return int(row[name])

    return Post(
        lang=row["lang"],
        text=row["text"] or "",
        entities_json=row["entities_json"] or "[]",
        photo_path=row["photo_path"] or "",
        account_id=int(row["account_id"] if "account_id" in keys else account_id),
        delivery=_s("delivery", "post") or "post",
        link_url=_s("link_url"),
        link_chat_id=_s("link_chat_id"),
        link_msg_id=_i("link_msg_id", 0),
        link_photo_url=_s("link_photo_url"),
        link_photo_chat_id=_s("link_photo_chat_id"),
        link_photo_msg_id=_i("link_photo_msg_id", 0),
    )


async def _migrate_posts_columns(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(posts)")
    cols = {row[1] for row in await cur.fetchall()}
    if not cols:
        return
    alters = {
        "delivery": "ALTER TABLE posts ADD COLUMN delivery TEXT NOT NULL DEFAULT 'post'",
        "link_url": "ALTER TABLE posts ADD COLUMN link_url TEXT NOT NULL DEFAULT ''",
        "link_chat_id": "ALTER TABLE posts ADD COLUMN link_chat_id TEXT NOT NULL DEFAULT ''",
        "link_msg_id": "ALTER TABLE posts ADD COLUMN link_msg_id INTEGER NOT NULL DEFAULT 0",
        "link_photo_url": "ALTER TABLE posts ADD COLUMN link_photo_url TEXT NOT NULL DEFAULT ''",
        "link_photo_chat_id": "ALTER TABLE posts ADD COLUMN link_photo_chat_id TEXT NOT NULL DEFAULT ''",
        "link_photo_msg_id": "ALTER TABLE posts ADD COLUMN link_photo_msg_id INTEGER NOT NULL DEFAULT 0",
    }
    for name, sql in alters.items():
        if name not in cols:
            await db.execute(sql)


async def _migrate_posts_table(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(posts)")
    cols = [row[1] for row in await cur.fetchall()]
    if not cols or "account_id" in cols:
        return
    cur = await db.execute("SELECT lang, text, entities_json, photo_path FROM posts")
    old_rows = await cur.fetchall()
    cur = await db.execute("SELECT id FROM accounts")
    acc_ids = [row[0] for row in await cur.fetchall()]
    await db.execute("ALTER TABLE posts RENAME TO posts_legacy")
    await db.execute(
        """
        CREATE TABLE posts (
            account_id INTEGER NOT NULL,
            lang TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            entities_json TEXT NOT NULL DEFAULT '[]',
            photo_path TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (account_id, lang)
        )
        """
    )
    for acc_id in acc_ids:
        for lang, text, ents, photo in old_rows:
            photo_path = photo or ""
            if photo_path:
                src = Path(photo_path)
                if src.exists():
                    dest_dir = POSTS_DIR / str(acc_id)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / src.name
                    if src.resolve() != dest.resolve():
                        copy2(src, dest)
                    photo_path = str(dest)
            await db.execute(
                "INSERT INTO posts(account_id, lang, text, entities_json, photo_path) "
                "VALUES(?, ?, ?, ?, ?)",
                (acc_id, lang, text or "", ents or "[]", photo_path),
            )
    await db.execute("DROP TABLE posts_legacy")


async def _migrate_accounts_premium(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(accounts)")
    cols = [row[1] for row in await cur.fetchall()]
    if not cols:
        return
    if "is_premium" not in cols:
        await db.execute(
            "ALTER TABLE accounts ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0"
        )
    if "online_ping" not in cols:
        await db.execute(
            "ALTER TABLE accounts ADD COLUMN online_ping INTEGER NOT NULL DEFAULT 1"
        )


async def _migrate_accounts_spam(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(accounts)")
    cols = {row[1] for row in await cur.fetchall()}
    if not cols:
        return
    for name in ("spam_status", "spam_checked_at", "spam_until", "spam_detail"):
        if name not in cols:
            await db.execute(f"ALTER TABLE accounts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


async def _migrate_chats_invite(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(chats)")
    cols = [row[1] for row in await cur.fetchall()]
    if not cols:
        return
    alters = {
        "invite_link": "ALTER TABLE chats ADD COLUMN invite_link TEXT NOT NULL DEFAULT ''",
        "captcha_kind": "ALTER TABLE chats ADD COLUMN captcha_kind TEXT NOT NULL DEFAULT 'auto'",
        "join_mode": "ALTER TABLE chats ADD COLUMN join_mode TEXT NOT NULL DEFAULT 'direct'",
        "garant_bot": "ALTER TABLE chats ADD COLUMN garant_bot TEXT NOT NULL DEFAULT ''",
        "after_join": "ALTER TABLE chats ADD COLUMN after_join TEXT NOT NULL DEFAULT 'none'",
        "after_join_bot": "ALTER TABLE chats ADD COLUMN after_join_bot TEXT NOT NULL DEFAULT ''",
        "require_channels": "ALTER TABLE chats ADD COLUMN require_channels TEXT NOT NULL DEFAULT ''",
        "is_join_request": "ALTER TABLE chats ADD COLUMN is_join_request INTEGER NOT NULL DEFAULT 0",
        "text_kind": "ALTER TABLE chats ADD COLUMN text_kind TEXT NOT NULL DEFAULT 'full'",
        "allow_media": "ALTER TABLE chats ADD COLUMN allow_media INTEGER NOT NULL DEFAULT 1",
    }
    for name, sql in alters.items():
        if name not in cols:
            await db.execute(sql)


def _account(row: aiosqlite.Row) -> Account:
    keys = row.keys()
    online_ping = 1
    if "online_ping" in keys and row["online_ping"] is not None:
        online_ping = int(row["online_ping"])
    return Account(
        id=row["id"],
        label=row["label"],
        phone=row["phone"] or "",
        username=row["username"] or "",
        user_id=row["user_id"],
        telethon_session=row["telethon_session"] or "",
        pyrogram_session=row["pyrogram_session"] or "",
        sender_bot_token=row["sender_bot_token"] or "",
        sender_account_id=row["sender_account_id"] or "",
        is_premium=int(row["is_premium"] if "is_premium" in keys else 0) or 0,
        online_ping=online_ping,
        status=row["status"] or "idle",
        last_error=row["last_error"] or "",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        spam_status=(row["spam_status"] if "spam_status" in keys else "") or "",
        spam_checked_at=(row["spam_checked_at"] if "spam_checked_at" in keys else "") or "",
        spam_until=(row["spam_until"] if "spam_until" in keys else "") or "",
        spam_detail=(row["spam_detail"] if "spam_detail" in keys else "") or "",
    )


def _restriction(row: aiosqlite.Row) -> Restriction:
    keys = row.keys()
    return Restriction(
        id=row["id"],
        account_id=row["account_id"],
        chat_pk=row["chat_pk"],
        kind=row["kind"],
        reason=row["reason"] or "",
        reason_link=row["reason_link"] or "",
        error=row["error"] or "",
        detected_at=row["detected_at"] or "",
        until_at=row["until_at"] or "",
        active=int(row["active"]),
        resolved_at=row["resolved_at"] or "",
        account_label=(row["account_label"] if "account_label" in keys else "") or "",
        chat_title=(row["chat_title"] if "chat_title" in keys else "") or "",
    )


def _chat(row: aiosqlite.Row) -> Chat:
    keys = row.keys()

    def _s(name: str, default: str = "") -> str:
        return (row[name] if name in keys else default) or default

    def _i(name: str, default: int = 0) -> int:
        if name not in keys or row[name] is None:
            return default
        return int(row[name])

    return Chat(
        id=row["id"],
        title=row["title"],
        chat_id=row["chat_id"],
        username=row["username"] or "",
        kind=row["kind"],
        lang=row["lang"],
        tag=row["tag"] or "",
        interval_minutes=int(row["interval_minutes"] or 60),
        enabled=int(row["enabled"] or 0),
        invite_link=_s("invite_link"),
        captcha_kind=_s("captcha_kind", "auto") or "auto",
        join_mode=_s("join_mode", "direct") or "direct",
        garant_bot=_s("garant_bot"),
        after_join=_s("after_join", "none") or "none",
        after_join_bot=_s("after_join_bot"),
        require_channels=_s("require_channels"),
        is_join_request=_i("is_join_request", 0),
        text_kind=_s("text_kind", "full") or "full",
        allow_media=_i("allow_media", 1),
        created_at=row["created_at"] or "",
    )


def _setup_state(row: aiosqlite.Row) -> SetupState:
    keys = row.keys()
    return SetupState(
        id=row["id"],
        account_id=row["account_id"],
        chat_pk=row["chat_pk"],
        status=row["status"] or "pending",
        fail_count=int(row["fail_count"] or 0),
        last_attempt_at=row["last_attempt_at"] or "",
        last_error=row["last_error"] or "",
        account_label=(row["account_label"] if "account_label" in keys else "") or "",
        chat_title=(row["chat_title"] if "chat_title" in keys else "") or "",
    )



def _incident(row: aiosqlite.Row) -> Incident:
    keys = row.keys()
    return Incident(
        id=row["id"],
        kind=row["kind"],
        severity=row["severity"] or "medium",
        title=row["title"] or "",
        detail=row["detail"] or "",
        status=row["status"] or "open",
        account_id=row["account_id"],
        chat_pk=row["chat_pk"],
        source=row["source"] or "marketer",
        first_seen_at=row["first_seen_at"] or "",
        last_seen_at=row["last_seen_at"] or "",
        resolved_at=row["resolved_at"] or "",
        meta_json=row["meta_json"] or "{}",
        account_label=(row["account_label"] if "account_label" in keys else "") or "",
        chat_title=(row["chat_title"] if "chat_title" in keys else "") or "",
    )


def _health_snapshot(row: aiosqlite.Row) -> HealthSnapshot:
    keys = row.keys()
    return HealthSnapshot(
        id=row["id"],
        account_id=row["account_id"],
        chat_pk=row["chat_pk"],
        checked_at=row["checked_at"] or "",
        status=row["status"] or "",
        count=int(row["count"] or 0),
        expected=int(row["expected"] or 0),
        error=row["error"] or "",
        sent_est=float(row["sent_est"] or 0),
        account_label=(row["account_label"] if "account_label" in keys else "") or "",
        chat_title=(row["chat_title"] if "chat_title" in keys else "") or "",
    )


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DB_PATH)

    async def init(self) -> None:
        ensure_dirs()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.execute("PRAGMA foreign_keys = ON")
            await _migrate_posts_table(db)
            await _migrate_posts_columns(db)
            await _migrate_accounts_premium(db)
            await _migrate_accounts_spam(db)
            await _migrate_chats_invite(db)
            for key, value in DEFAULT_SETTINGS.items():
                await db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, value),
                )
            await db.commit()

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path)

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()

    async def sender_settings(self) -> SenderSettings:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT key, value FROM settings")
            rows = {r["key"]: r["value"] for r in await cur.fetchall()}

        def _i(key: str, default: int) -> int:
            try:
                return int(rows.get(key, default))
            except (TypeError, ValueError):
                return default

        return SenderSettings(
            between_min=_i("between_min", 30),
            between_max=_i("between_max", 90),
            cycle_min=_i("cycle_min", 300),
            cycle_max=_i("cycle_max", 600),
            per_chat_min=_i("per_chat_min", 3600),
            per_chat_max=_i("per_chat_max", 3600),
            parallel=_i("parallel", 4),
            cloak_enabled=str(rows.get("cloak_enabled", "0")) not in {"0", "false", "off", ""},
            cloak_text=rows.get("cloak_text", "") or "",
            cloak_entities_json=rows.get("cloak_entities_json", "[]") or "[]",
            mentions_enabled=str(rows.get("mentions_enabled", "0"))
            not in {"0", "false", "off", ""},
            keep_extra_ids=rows.get("keep_extra_ids", "") or "",
        )

    async def update_sender_settings(self, **kwargs: Any) -> SenderSettings:
        mapping = {
            "between_min": "between_min",
            "between_max": "between_max",
            "cycle_min": "cycle_min",
            "cycle_max": "cycle_max",
            "per_chat_min": "per_chat_min",
            "per_chat_max": "per_chat_max",
            "parallel": "parallel",
            "cloak_enabled": "cloak_enabled",
            "cloak_text": "cloak_text",
            "cloak_entities_json": "cloak_entities_json",
            "mentions_enabled": "mentions_enabled",
            "keep_extra_ids": "keep_extra_ids",
        }
        for key, value in kwargs.items():
            if key not in mapping:
                continue
            stored = value
            if isinstance(value, bool):
                stored = "1" if value else "0"
            await self.set_setting(mapping[key], str(stored))
        return await self.sender_settings()

    async def online_ping_settings(self) -> OnlinePingSettings:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT key, value FROM settings")
            rows = {r["key"]: r["value"] for r in await cur.fetchall()}

        def _f(key: str, default: float) -> float:
            try:
                return float(rows.get(key, default))
            except (TypeError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            try:
                return int(float(rows.get(key, default)))
            except (TypeError, ValueError):
                return default

        return OnlinePingSettings(
            enabled=str(rows.get("online_ping_enabled", "1")) not in {"0", "false", "off"},
            hours=max(0.5, _f("online_ping_hours", 3.5)),
            jitter_sec=max(0, _i("online_ping_jitter_sec", 1800)),
            hold_seconds=max(0.5, _f("online_hold_seconds", 4.0)),
            next_at=rows.get("online_ping_next_at", "") or "",
            last_at=rows.get("online_ping_last_at", "") or "",
        )

    async def update_online_ping_settings(self, **kwargs: Any) -> OnlinePingSettings:
        mapping = {
            "enabled": "online_ping_enabled",
            "hours": "online_ping_hours",
            "jitter_sec": "online_ping_jitter_sec",
            "hold_seconds": "online_hold_seconds",
            "next_at": "online_ping_next_at",
            "last_at": "online_ping_last_at",
        }
        for key, value in kwargs.items():
            if key not in mapping:
                continue
            stored = value
            if isinstance(value, bool):
                stored = "1" if value else "0"
            await self.set_setting(mapping[key], str(stored))
        return await self.online_ping_settings()

    async def list_accounts(self) -> list[Account]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM accounts ORDER BY id")
            return [_account(r) for r in await cur.fetchall()]

    async def get_account(self, account_id: int) -> Account | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
            row = await cur.fetchone()
            return _account(row) if row else None

    async def add_account(self, label: str) -> Account:
        now = _now()
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT INTO accounts(label, created_at, updated_at) VALUES(?, ?, ?)",
                (label, now, now),
            )
            await db.commit()
            pk = cur.lastrowid
        account = await self.get_account(int(pk))
        assert account is not None
        return account

    async def update_account(self, account_id: int, **fields: Any) -> Account | None:
        if not fields:
            return await self.get_account(account_id)
        fields = {k: v for k, v in fields.items() if v is not None}
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [account_id]
        async with self._connect() as db:
            await db.execute(f"UPDATE accounts SET {cols} WHERE id=?", values)
            await db.commit()
        return await self.get_account(account_id)

    async def delete_account(self, account_id: int) -> None:
        async with self._connect() as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("DELETE FROM setup_states WHERE account_id=?", (account_id,))
            await db.execute("DELETE FROM minute_slots WHERE account_id=?", (account_id,))
            await db.execute("DELETE FROM posts WHERE account_id=?", (account_id,))
            for table in (
                "send_events",
                "send_scans",
                "chat_restrictions",
                "account_chat_prefs",
                "account_chat_posts",
            ):
                await db.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
            await db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            await db.commit()

    async def get_post(self, account_id: int, lang: str) -> Post:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM posts WHERE account_id=? AND lang=?",
                (account_id, lang),
            )
            return _post(await cur.fetchone(), account_id=account_id, lang=lang)

    async def save_post(
        self,
        account_id: int,
        lang: str,
        text: str,
        entities: list[dict[str, Any]],
        photo_path: str | None = None,
        *,
        delivery: str | None = None,
        clear_photo: bool = False,
    ) -> Post:
        existing = await self.get_post(account_id, lang)
        if clear_photo:
            photo = ""
        elif photo_path is not None:
            photo = photo_path
        else:
            photo = existing.photo_path
        deliv = delivery if delivery is not None else (existing.delivery or "post")
        if deliv not in {"post", "link"}:
            deliv = "post"
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO posts(account_id, lang, text, entities_json, photo_path, delivery) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id, lang) DO UPDATE SET text=excluded.text, "
                "entities_json=excluded.entities_json, photo_path=excluded.photo_path, "
                "delivery=excluded.delivery",
                (
                    account_id,
                    lang,
                    text,
                    json.dumps(entities, ensure_ascii=False),
                    photo,
                    deliv,
                ),
            )
            await db.commit()
        return await self.get_post(account_id, lang)

    async def save_post_link(
        self,
        account_id: int,
        lang: str,
        *,
        kind: str = "text",  # text | photo
        url: str,
        chat_ref: str | int,
        msg_id: int,
    ) -> Post:
        """Сохранить ссылку на сообщение канала (text или photo вариант)."""
        existing = await self.get_post(account_id, lang)
        chat_s = str(chat_ref).strip()
        msg_i = int(msg_id)
        url_s = (url or "").strip()
        async with self._connect() as db:
            if kind == "photo":
                await db.execute(
                    "INSERT INTO posts(account_id, lang, delivery, "
                    "link_photo_url, link_photo_chat_id, link_photo_msg_id, "
                    "text, entities_json, photo_path) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account_id, lang) DO UPDATE SET "
                    "delivery='link', "
                    "link_photo_url=excluded.link_photo_url, "
                    "link_photo_chat_id=excluded.link_photo_chat_id, "
                    "link_photo_msg_id=excluded.link_photo_msg_id",
                    (
                        account_id,
                        lang,
                        "link",
                        url_s,
                        chat_s,
                        msg_i,
                        existing.text or "",
                        existing.entities_json or "[]",
                        existing.photo_path or "",
                    ),
                )
            else:
                await db.execute(
                    "INSERT INTO posts(account_id, lang, delivery, "
                    "link_url, link_chat_id, link_msg_id, "
                    "text, entities_json, photo_path) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account_id, lang) DO UPDATE SET "
                    "delivery='link', "
                    "link_url=excluded.link_url, "
                    "link_chat_id=excluded.link_chat_id, "
                    "link_msg_id=excluded.link_msg_id",
                    (
                        account_id,
                        lang,
                        "link",
                        url_s,
                        chat_s,
                        msg_i,
                        existing.text or "",
                        existing.entities_json or "[]",
                        existing.photo_path or "",
                    ),
                )
            await db.commit()
        return await self.get_post(account_id, lang)

    async def set_posts_delivery(self, account_id: int, delivery: str) -> None:
        deliv = "link" if delivery == "link" else "post"
        langs = ("ru", "en", "ru_short", "en_short")
        async with self._connect() as db:
            for lang in langs:
                await db.execute(
                    "INSERT INTO posts(account_id, lang, delivery) VALUES(?,?,?) "
                    "ON CONFLICT(account_id, lang) DO UPDATE SET delivery=excluded.delivery",
                    (account_id, lang, deliv),
                )
            await db.commit()

    async def list_chats(self, kind: str | None = None, enabled_only: bool = False) -> list[Chat]:
        sql = "SELECT * FROM chats WHERE 1=1"
        args: list[Any] = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY kind DESC, title COLLATE NOCASE"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            return [_chat(r) for r in await cur.fetchall()]

    async def get_chat(self, chat_pk: int) -> Chat | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM chats WHERE id=?", (chat_pk,))
            row = await cur.fetchone()
            return _chat(row) if row else None

    async def get_chat_by_tg(self, chat_id: str) -> Chat | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM chats WHERE chat_id=?", (str(chat_id),))
            row = await cur.fetchone()
            return _chat(row) if row else None

    async def add_chat(
        self,
        title: str,
        chat_id: str,
        username: str = "",
        kind: str = "sender",
        lang: str = "ru",
        tag: str = "",
        interval_minutes: int = 60,
        invite_link: str = "",
        captcha_kind: str = "auto",
        join_mode: str = "direct",
        garant_bot: str = "",
        after_join: str = "none",
        after_join_bot: str = "",
        require_channels: str = "",
        is_join_request: int = 0,
    ) -> Chat:
        from app.tg.join_presets import match_preset

        preset = match_preset(
            chat_id=str(chat_id), title=title, username=username, invite_link=invite_link
        )
        if preset:
            captcha_kind = captcha_kind if captcha_kind != "auto" else preset.get("captcha_kind", captcha_kind)
            join_mode = join_mode if join_mode != "direct" else preset.get("join_mode", join_mode)
            garant_bot = garant_bot or preset.get("garant_bot", "")
            after_join = after_join if after_join != "none" else preset.get("after_join", after_join)
            after_join_bot = after_join_bot or preset.get("after_join_bot", "")
            require_channels = require_channels or preset.get("require_channels", "")
            is_join_request = is_join_request or int(preset.get("is_join_request") or 0)

        async with self._connect() as db:
            cur = await db.execute(
                "INSERT INTO chats(title, chat_id, username, kind, lang, tag, "
                "interval_minutes, enabled, invite_link, captcha_kind, join_mode, "
                "garant_bot, after_join, after_join_bot, require_channels, "
                "is_join_request, created_at) "
                "VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)",
                (
                    title,
                    str(chat_id),
                    username or "",
                    kind,
                    lang,
                    tag or "",
                    int(interval_minutes),
                    invite_link or "",
                    captcha_kind or "auto",
                    join_mode or "direct",
                    (garant_bot or "").lstrip("@"),
                    after_join or "none",
                    (after_join_bot or "").lstrip("@"),
                    require_channels or "",
                    int(is_join_request or 0),
                    _now(),
                ),
            )
            await db.commit()
            pk = cur.lastrowid
        chat = await self.get_chat(int(pk))
        assert chat is not None
        return chat

    async def update_chat(self, chat_pk: int, **fields: Any) -> Chat | None:
        allowed = {
            "title",
            "chat_id",
            "username",
            "kind",
            "lang",
            "tag",
            "interval_minutes",
            "enabled",
            "invite_link",
            "captcha_kind",
            "join_mode",
            "garant_bot",
            "after_join",
            "after_join_bot",
            "require_channels",
            "is_join_request",
            "text_kind",
            "allow_media",
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if "garant_bot" in fields and fields["garant_bot"] is not None:
            fields["garant_bot"] = str(fields["garant_bot"]).lstrip("@")
        if "after_join_bot" in fields and fields["after_join_bot"] is not None:
            fields["after_join_bot"] = str(fields["after_join_bot"]).lstrip("@")
        if "text_kind" in fields and fields["text_kind"] is not None:
            raw = str(fields["text_kind"]).strip().casefold()
            fields["text_kind"] = "short" if raw in {"short", "короткий", "1", "true"} else "full"
        if not fields:
            return await self.get_chat(chat_pk)
        cols = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [chat_pk]
        async with self._connect() as db:
            await db.execute(f"UPDATE chats SET {cols} WHERE id=?", values)
            await db.commit()
        return await self.get_chat(chat_pk)

    async def delete_chat(self, chat_pk: int) -> None:
        async with self._connect() as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("DELETE FROM setup_states WHERE chat_pk=?", (chat_pk,))
            await db.execute("DELETE FROM minute_slots WHERE chat_pk=?", (chat_pk,))
            await db.execute("DELETE FROM send_events WHERE chat_pk=?", (chat_pk,))
            await db.execute("DELETE FROM send_scans WHERE chat_pk=?", (chat_pk,))
            await db.execute("DELETE FROM chat_restrictions WHERE chat_pk=?", (chat_pk,))
            await db.execute("DELETE FROM chats WHERE id=?", (chat_pk,))
            await db.commit()

    async def get_setup_state(self, account_id: int, chat_pk: int) -> SetupState | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, COALESCE(a.label,'') AS account_label, "
                "COALESCE(c.title,'') AS chat_title "
                "FROM setup_states s "
                "LEFT JOIN accounts a ON a.id=s.account_id "
                "LEFT JOIN chats c ON c.id=s.chat_pk "
                "WHERE s.account_id=? AND s.chat_pk=?",
                (account_id, chat_pk),
            )
            row = await cur.fetchone()
        return _setup_state(row) if row else None

    async def record_setup_ok(self, account_id: int, chat_pk: int) -> SetupState:
        now = _now()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO setup_states(account_id, chat_pk, status, fail_count, "
                "last_attempt_at, last_error) VALUES(?, ?, 'ok', 0, ?, '') "
                "ON CONFLICT(account_id, chat_pk) DO UPDATE SET "
                "status='ok', fail_count=0, last_attempt_at=excluded.last_attempt_at, "
                "last_error=''",
                (account_id, chat_pk, now),
            )
            await db.commit()
        state = await self.get_setup_state(account_id, chat_pk)
        assert state is not None
        return state

    async def record_setup_fail(
        self,
        account_id: int,
        chat_pk: int,
        error: str,
        *,
        max_attempts: int = 3,
    ) -> SetupState:
        now = _now()
        existing = await self.get_setup_state(account_id, chat_pk)
        if existing is not None and existing.is_abandoned:
            # уже «стоп до начала недели»: счётчик не растёт (раньше доходил до 130/3)
            fail_count = existing.fail_count
            status = "abandoned"
        else:
            fail_count = (existing.fail_count if existing else 0) + 1
            status = "abandoned" if fail_count >= max_attempts else "pending"
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO setup_states(account_id, chat_pk, status, fail_count, "
                "last_attempt_at, last_error) VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id, chat_pk) DO UPDATE SET "
                "status=excluded.status, fail_count=excluded.fail_count, "
                "last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error",
                (account_id, chat_pk, status, fail_count, now, (error or "")[:500]),
            )
            await db.commit()
        state = await self.get_setup_state(account_id, chat_pk)
        assert state is not None
        return state

    async def list_setup_states(
        self,
        account_id: int | None = None,
        *,
        statuses: list[str] | None = None,
    ) -> list[SetupState]:
        sql = (
            "SELECT s.*, COALESCE(a.label,'') AS account_label, "
            "COALESCE(c.title,'') AS chat_title "
            "FROM setup_states s "
            "LEFT JOIN accounts a ON a.id=s.account_id "
            "LEFT JOIN chats c ON c.id=s.chat_pk "
            "WHERE 1=1"
        )
        args: list[Any] = []
        if account_id is not None:
            sql += " AND s.account_id=?"
            args.append(account_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND s.status IN ({placeholders})"
            args.extend(statuses)
        sql += " ORDER BY s.account_id, c.title COLLATE NOCASE"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            return [_setup_state(r) for r in await cur.fetchall()]

    async def list_due_setup_retries(
        self,
        *,
        retry_days: int = 3,
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> list[SetupState]:
        """Pending setup states ready for another attempt.

        - Never tried this cycle (fail_count=0 after weekly reset) → due now
        - Already failed, last attempt ≥ retry_days ago, fail_count < max → due
        """
        from datetime import timedelta

        moment = now or datetime.now(timezone.utc)
        cutoff = (moment - timedelta(days=retry_days)).isoformat(timespec="seconds")
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, COALESCE(a.label,'') AS account_label, "
                "COALESCE(c.title,'') AS chat_title "
                "FROM setup_states s "
                "JOIN chats c ON c.id=s.chat_pk "
                "LEFT JOIN accounts a ON a.id=s.account_id "
                "WHERE s.status='pending' "
                "AND s.fail_count < ? "
                "AND c.enabled=1 AND c.kind='schedule' "
                "AND ("
                "  s.fail_count = 0 "
                "  OR (s.last_attempt_at <> '' AND s.last_attempt_at <= ?)"
                ") "
                "ORDER BY s.account_id, c.title COLLATE NOCASE",
                (max_attempts, cutoff),
            )
            return [_setup_state(r) for r in await cur.fetchall()]

    async def reset_setup_states_for_weekly(
        self,
        *,
        include_abandoned: bool = True,
    ) -> int:
        """Clear fail counters so weekly pass can retry missing chats again."""
        statuses = ["pending"]
        if include_abandoned:
            statuses.append("abandoned")
        placeholders = ",".join("?" for _ in statuses)
        async with self._connect() as db:
            cur = await db.execute(
                f"UPDATE setup_states SET status='pending', fail_count=0, "
                f"last_error='' WHERE status IN ({placeholders})",
                statuses,
            )
            await db.commit()
            return int(cur.rowcount or 0)


    async def active_slots(self) -> list[MinuteSlot]:
        """Только слоты, где schedule реально назначен (setup_states.status=ok)."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, a.label AS account_label, c.title AS chat_title "
                "FROM minute_slots s "
                "JOIN accounts a ON a.id=s.account_id "
                "JOIN chats c ON c.id=s.chat_pk "
                "JOIN setup_states st ON st.account_id=s.account_id AND st.chat_pk=s.chat_pk "
                "WHERE st.status='ok' "
                "ORDER BY c.title COLLATE NOCASE, s.start_minute"
            )
            rows = await cur.fetchall()
        return [
            MinuteSlot(
                id=r["id"],
                chat_pk=r["chat_pk"],
                account_id=r["account_id"],
                start_minute=int(r["start_minute"]),
                account_label=r["account_label"] or "",
                chat_title=r["chat_title"] or "",
            )
            for r in rows
        ]

    async def active_slots_for_chat(self, chat_pk: int) -> list[MinuteSlot]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, a.label AS account_label, c.title AS chat_title "
                "FROM minute_slots s "
                "JOIN accounts a ON a.id=s.account_id "
                "JOIN chats c ON c.id=s.chat_pk "
                "JOIN setup_states st ON st.account_id=s.account_id AND st.chat_pk=s.chat_pk "
                "WHERE s.chat_pk=? AND st.status='ok' "
                "ORDER BY s.start_minute",
                (chat_pk,),
            )
            rows = await cur.fetchall()
        return [
            MinuteSlot(
                id=r["id"],
                chat_pk=r["chat_pk"],
                account_id=r["account_id"],
                start_minute=int(r["start_minute"]),
                account_label=r["account_label"] or "",
                chat_title=r["chat_title"] or "",
            )
            for r in rows
        ]

    async def slots_for_chat(self, chat_pk: int) -> list[MinuteSlot]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, a.label AS account_label, c.title AS chat_title "
                "FROM minute_slots s "
                "JOIN accounts a ON a.id=s.account_id "
                "JOIN chats c ON c.id=s.chat_pk "
                "WHERE s.chat_pk=? ORDER BY s.start_minute",
                (chat_pk,),
            )
            rows = await cur.fetchall()
        return [
            MinuteSlot(
                id=r["id"],
                chat_pk=r["chat_pk"],
                account_id=r["account_id"],
                start_minute=int(r["start_minute"]),
                account_label=r["account_label"] or "",
                chat_title=r["chat_title"] or "",
            )
            for r in rows
        ]

    async def all_slots(self) -> list[MinuteSlot]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, a.label AS account_label, c.title AS chat_title "
                "FROM minute_slots s "
                "JOIN accounts a ON a.id=s.account_id "
                "JOIN chats c ON c.id=s.chat_pk "
                "ORDER BY c.title COLLATE NOCASE, s.start_minute"
            )
            rows = await cur.fetchall()
        return [
            MinuteSlot(
                id=r["id"],
                chat_pk=r["chat_pk"],
                account_id=r["account_id"],
                start_minute=int(r["start_minute"]),
                account_label=r["account_label"] or "",
                chat_title=r["chat_title"] or "",
            )
            for r in rows
        ]

    async def slot_for(self, chat_pk: int, account_id: int) -> MinuteSlot | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, a.label AS account_label, c.title AS chat_title "
                "FROM minute_slots s "
                "JOIN accounts a ON a.id=s.account_id "
                "JOIN chats c ON c.id=s.chat_pk "
                "WHERE s.chat_pk=? AND s.account_id=?",
                (chat_pk, account_id),
            )
            r = await cur.fetchone()
        if not r:
            return None
        return MinuteSlot(
            id=r["id"],
            chat_pk=r["chat_pk"],
            account_id=r["account_id"],
            start_minute=int(r["start_minute"]),
            account_label=r["account_label"] or "",
            chat_title=r["chat_title"] or "",
        )

    async def set_slot(self, chat_pk: int, account_id: int, start_minute: int) -> MinuteSlot:
        minute = int(start_minute) % 60
        async with self._connect() as db:
            try:
                await db.execute(
                    "INSERT INTO minute_slots(chat_pk, account_id, start_minute) VALUES(?,?,?) "
                    "ON CONFLICT(chat_pk, account_id) DO UPDATE SET start_minute=excluded.start_minute",
                    (chat_pk, account_id, minute),
                )
                await db.commit()
            except aiosqlite.IntegrityError as e:
                raise ValueError("Эта минута уже занята другим аккаунтом в этом чате") from e
        slot = await self.slot_for(chat_pk, account_id)
        assert slot is not None
        return slot

    async def delete_slot(self, chat_pk: int, account_id: int) -> None:
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM minute_slots WHERE chat_pk=? AND account_id=?",
                (chat_pk, account_id),
            )
            await db.commit()

    async def occupied_minutes(self, chat_pk: int, exclude_account: int | None = None) -> list[int]:
        slots = await self.slots_for_chat(chat_pk)
        return [
            s.start_minute
            for s in slots
            if exclude_account is None or s.account_id != exclude_account
        ]

    async def create_job(self, kind: str, account_id: int | None) -> Job:
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT INTO jobs(account_id, kind, status, started_at) VALUES(?,?,?,?)",
                (account_id, kind, "running", _now()),
            )
            await db.commit()
            pk = cur.lastrowid
        job = await self.get_job(int(pk))
        assert job is not None
        return job

    async def get_job(self, job_id: int) -> Job | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT j.*, COALESCE(a.label,'') AS account_label "
                "FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id WHERE j.id=?",
                (job_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return Job(
            id=row["id"],
            account_id=row["account_id"],
            kind=row["kind"],
            status=row["status"],
            started_at=row["started_at"] or "",
            finished_at=row["finished_at"] or "",
            report=row["report"] or "",
            account_label=row["account_label"] or "",
        )

    async def finish_job(self, job_id: int, status: str, report: str = "") -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE jobs SET status=?, finished_at=?, report=? WHERE id=?",
                (status, _now(), report, job_id),
            )
            await db.commit()

    async def add_log(self, job_id: int, message: str, level: str = "info") -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO job_logs(job_id, created_at, level, message) VALUES(?,?,?,?)",
                (job_id, _now(), level, message),
            )
            await db.commit()

    async def job_logs(self, job_id: int) -> list[JobLog]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM job_logs WHERE job_id=? ORDER BY id",
                (job_id,),
            )
            rows = await cur.fetchall()
        return [
            JobLog(
                id=r["id"],
                job_id=r["job_id"],
                created_at=r["created_at"],
                level=r["level"],
                message=r["message"],
            )
            for r in rows
        ]

    async def recent_jobs(self, limit: int = 20) -> list[Job]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT j.*, COALESCE(a.label,'') AS account_label "
                "FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id "
                "ORDER BY j.id DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            Job(
                id=r["id"],
                account_id=r["account_id"],
                kind=r["kind"],
                status=r["status"],
                started_at=r["started_at"] or "",
                finished_at=r["finished_at"] or "",
                report=r["report"] or "",
                account_label=r["account_label"] or "",
            )
            for r in rows
        ]

    # ---- setup state helpers -------------------------------------------------

    async def reset_setup_state(self, account_id: int, chat_pk: int) -> None:
        """Пара снова может пробоваться (например, после вступления в чат)."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE setup_states SET status='pending', fail_count=0, last_error='' "
                "WHERE account_id=? AND chat_pk=? AND status<>'ok'",
                (account_id, chat_pk),
            )
            await db.commit()

    # ---- bans / mutes --------------------------------------------------------

    async def upsert_restriction(
        self,
        account_id: int,
        chat_pk: int,
        kind: str,
        *,
        reason: str = "",
        reason_link: str = "",
        error: str = "",
        until_at: str = "",
    ) -> Restriction:
        now = _now()
        existing = await self.get_restriction(account_id, chat_pk, active_only=False)
        keep_since = (
            existing.detected_at
            if existing and existing.active and existing.kind == kind
            else now
        )
        reason = reason or (existing.reason if existing and existing.active and existing.kind == kind else "")
        reason_link = reason_link or (
            existing.reason_link if existing and existing.active and existing.kind == kind else ""
        )
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO chat_restrictions(account_id, chat_pk, kind, reason, "
                "reason_link, error, detected_at, until_at, active, resolved_at) "
                "VALUES(?,?,?,?,?,?,?,?,1,'') "
                "ON CONFLICT(account_id, chat_pk) DO UPDATE SET "
                "kind=excluded.kind, reason=excluded.reason, reason_link=excluded.reason_link, "
                "error=excluded.error, detected_at=excluded.detected_at, "
                "until_at=excluded.until_at, active=1, resolved_at=''",
                (
                    account_id,
                    chat_pk,
                    kind,
                    reason[:1500],
                    reason_link,
                    (error or "")[:500],
                    keep_since,
                    until_at,
                ),
            )
            await db.commit()
        got = await self.get_restriction(account_id, chat_pk, active_only=False)
        assert got is not None
        return got

    async def resolve_restriction(self, account_id: int, chat_pk: int) -> bool:
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE chat_restrictions SET active=0, resolved_at=? "
                "WHERE account_id=? AND chat_pk=? AND active=1",
                (_now(), account_id, chat_pk),
            )
            await db.commit()
            return bool(cur.rowcount)

    async def get_restriction(
        self, account_id: int, chat_pk: int, *, active_only: bool = True
    ) -> Restriction | None:
        sql = (
            "SELECT r.*, COALESCE(a.label,'') AS account_label, "
            "COALESCE(c.title,'') AS chat_title FROM chat_restrictions r "
            "LEFT JOIN accounts a ON a.id=r.account_id "
            "LEFT JOIN chats c ON c.id=r.chat_pk "
            "WHERE r.account_id=? AND r.chat_pk=?"
        )
        if active_only:
            sql += " AND r.active=1"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, (account_id, chat_pk))
            row = await cur.fetchone()
        return _restriction(row) if row else None

    async def list_restrictions(
        self,
        *,
        kinds: tuple[str, ...] | None = None,
        account_id: int | None = None,
        chat_pk: int | None = None,
        active_only: bool = True,
    ) -> list[Restriction]:
        sql = (
            "SELECT r.*, COALESCE(a.label,'') AS account_label, "
            "COALESCE(c.title,'') AS chat_title FROM chat_restrictions r "
            "LEFT JOIN accounts a ON a.id=r.account_id "
            "LEFT JOIN chats c ON c.id=r.chat_pk WHERE 1=1"
        )
        args: list[Any] = []
        if active_only:
            sql += " AND r.active=1"
        if kinds:
            sql += " AND r.kind IN (" + ",".join("?" for _ in kinds) + ")"
            args.extend(kinds)
        if account_id is not None:
            sql += " AND r.account_id=?"
            args.append(account_id)
        if chat_pk is not None:
            sql += " AND r.chat_pk=?"
            args.append(chat_pk)
        sql += " ORDER BY a.label COLLATE NOCASE, c.title COLLATE NOCASE"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            return [_restriction(r) for r in await cur.fetchall()]

    async def banned_pairs(self) -> set[tuple[int, int]]:
        rows = await self.list_restrictions(kinds=("ban",))
        return {(r.account_id, r.chat_pk) for r in rows}

    # ---- per account × chat prefs -------------------------------------------

    async def get_pref(self, account_id: int, chat_ref: str | int) -> ChatPref:
        key = canon_chat_id(chat_ref)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM account_chat_prefs WHERE account_id=? AND chat_key=?",
                (account_id, key),
            )
            row = await cur.fetchone()
        if not row:
            return ChatPref(account_id=account_id, chat_key=key)
        return ChatPref(
            account_id=account_id,
            chat_key=key,
            enabled=int(row["enabled"]),
            mention=int(row["mention"]),
        )

    async def set_pref(
        self,
        account_id: int,
        chat_ref: str | int,
        *,
        enabled: bool | None = None,
        mention: int | None = None,
    ) -> ChatPref:
        cur_pref = await self.get_pref(account_id, chat_ref)
        new_enabled = cur_pref.enabled if enabled is None else int(bool(enabled))
        new_mention = cur_pref.mention if mention is None else int(mention)
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO account_chat_prefs(account_id, chat_key, enabled, mention) "
                "VALUES(?,?,?,?) ON CONFLICT(account_id, chat_key) DO UPDATE SET "
                "enabled=excluded.enabled, mention=excluded.mention",
                (account_id, cur_pref.chat_key, new_enabled, new_mention),
            )
            await db.commit()
        return ChatPref(account_id, cur_pref.chat_key, new_enabled, new_mention)

    async def list_prefs(self, account_id: int | None = None) -> list[ChatPref]:
        sql = "SELECT * FROM account_chat_prefs"
        args: list[Any] = []
        if account_id is not None:
            sql += " WHERE account_id=?"
            args.append(account_id)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            rows = await cur.fetchall()
        return [
            ChatPref(r["account_id"], r["chat_key"], int(r["enabled"]), int(r["mention"]))
            for r in rows
        ]

    async def disabled_pairs(self) -> set[tuple[int, str]]:
        """(account_id, canon chat key) — отправка выключена вручную."""
        return {(p.account_id, p.chat_key) for p in await self.list_prefs() if not p.enabled}

    # ---- per account × chat custom text -------------------------------------

    async def get_chat_post(self, account_id: int, chat_ref: str | int) -> Post | None:
        key = canon_chat_id(chat_ref)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM account_chat_posts WHERE account_id=? AND chat_key=?",
                (account_id, key),
            )
            row = await cur.fetchone()
        if not row:
            return None
        if not (row["text"] or "").strip() and not (row["photo_path"] or "").strip():
            return None
        return Post(
            lang="custom",
            text=row["text"] or "",
            entities_json=row["entities_json"] or "[]",
            photo_path=row["photo_path"] or "",
            account_id=account_id,
        )

    async def save_chat_post(
        self,
        account_id: int,
        chat_ref: str | int,
        text: str,
        entities: list[dict] | None,
        photo_path: str = "",
    ) -> None:
        key = canon_chat_id(chat_ref)
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO account_chat_posts(account_id, chat_key, text, entities_json, photo_path) "
                "VALUES(?,?,?,?,?) ON CONFLICT(account_id, chat_key) DO UPDATE SET "
                "text=excluded.text, entities_json=excluded.entities_json, "
                "photo_path=excluded.photo_path",
                (account_id, key, text or "", json.dumps(entities or [], ensure_ascii=False), photo_path or ""),
            )
            await db.commit()

    async def delete_chat_post(self, account_id: int, chat_ref: str | int) -> None:
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM account_chat_posts WHERE account_id=? AND chat_key=?",
                (account_id, canon_chat_id(chat_ref)),
            )
            await db.commit()

    async def chat_post_keys(self, account_id: int) -> set[str]:
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT chat_key FROM account_chat_posts WHERE account_id=? "
                "AND (text<>'' OR photo_path<>'')",
                (account_id,),
            )
            return {r[0] for r in await cur.fetchall()}

    # ---- send facts ----------------------------------------------------------

    async def add_send_events(self, rows: list[tuple[int, int, str, int]]) -> int:
        """rows: (account_id, chat_pk, sent_at_utc_iso, msg_id)."""
        if not rows:
            return 0
        async with self._connect() as db:
            before = (await (await db.execute("SELECT COUNT(*) FROM send_events")).fetchone())[0]
            await db.executemany(
                "INSERT OR IGNORE INTO send_events(account_id, chat_pk, sent_at, msg_id) "
                "VALUES(?,?,?,?)",
                rows,
            )
            await db.commit()
            after = (await (await db.execute("SELECT COUNT(*) FROM send_events")).fetchone())[0]
        return int(after - before)

    async def send_events_between(
        self, start_utc: str, end_utc: str
    ) -> list[tuple[int, int, str]]:
        """[(account_id, chat_pk, sent_at)] для start <= sent_at < end."""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT account_id, chat_pk, sent_at FROM send_events "
                "WHERE sent_at>=? AND sent_at<? ORDER BY sent_at",
                (start_utc, end_utc),
            )
            return [(r[0], r[1], r[2]) for r in await cur.fetchall()]

    async def mark_scan(self, account_id: int, chat_pk: int, status: str, detail: str = "") -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO send_scans(account_id, chat_pk, scanned_at, status, detail) "
                "VALUES(?,?,?,?,?) ON CONFLICT(account_id, chat_pk) DO UPDATE SET "
                "scanned_at=excluded.scanned_at, status=excluded.status, detail=excluded.detail",
                (account_id, chat_pk, _now(), status, detail[:300]),
            )
            await db.commit()

    async def list_scans(self) -> list[tuple[int, int, str, str, str]]:
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT account_id, chat_pk, scanned_at, status, detail FROM send_scans"
            )
            return [tuple(r) for r in await cur.fetchall()]  # type: ignore[misc]

    async def last_send_at(self) -> str:
        async with self._connect() as db:
            cur = await db.execute("SELECT MAX(sent_at) FROM send_events")
            row = await cur.fetchone()
        return (row[0] if row and row[0] else "") or ""

    async def prune_send_events(self, keep_days: int = 90) -> int:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(
            timespec="seconds"
        )
        async with self._connect() as db:
            cur = await db.execute("DELETE FROM send_events WHERE sent_at<?", (cutoff,))
            await db.commit()
            return int(cur.rowcount or 0)

    async def upsert_incident(
        self,
        *,
        kind: str,
        severity: str,
        title: str,
        detail: str = "",
        account_id: int | None = None,
        chat_pk: int | None = None,
        source: str = "marketer",
        meta_json: str = "{}",
    ) -> Incident:
        """Dedup open incidents by (kind, account_id, chat_pk); bump last_seen."""
        now = _now()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id FROM incidents WHERE kind=? AND status IN ('open','fixing') "
                "AND IFNULL(account_id,-1)=IFNULL(?, -1) "
                "AND IFNULL(chat_pk,-1)=IFNULL(?, -1) "
                "ORDER BY id DESC LIMIT 1",
                (kind, account_id, chat_pk),
            )
            row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE incidents SET severity=?, title=?, detail=?, last_seen_at=?, "
                    "meta_json=?, source=? WHERE id=?",
                    (
                        severity,
                        title,
                        (detail or "")[:2000],
                        now,
                        meta_json or "{}",
                        source,
                        row["id"],
                    ),
                )
                await db.commit()
                pk = int(row["id"])
            else:
                cur = await db.execute(
                    "INSERT INTO incidents(account_id, chat_pk, kind, severity, title, "
                    "detail, status, source, first_seen_at, last_seen_at, meta_json) "
                    "VALUES(?,?,?,?,?,?, 'open', ?,?,?,?)",
                    (
                        account_id,
                        chat_pk,
                        kind,
                        severity,
                        title,
                        (detail or "")[:2000],
                        source,
                        now,
                        now,
                        meta_json or "{}",
                    ),
                )
                await db.commit()
                pk = int(cur.lastrowid)
        inc = await self.get_incident(pk)
        assert inc is not None
        return inc

    async def get_incident(self, incident_id: int) -> Incident | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT i.*, COALESCE(a.label,'') AS account_label, "
                "COALESCE(c.title,'') AS chat_title "
                "FROM incidents i "
                "LEFT JOIN accounts a ON a.id=i.account_id "
                "LEFT JOIN chats c ON c.id=i.chat_pk "
                "WHERE i.id=?",
                (incident_id,),
            )
            row = await cur.fetchone()
        return _incident(row) if row else None

    async def list_incidents(
        self,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[Incident]:
        statuses = statuses or ["open", "fixing"]
        placeholders = ",".join("?" for _ in statuses)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT i.*, COALESCE(a.label,'') AS account_label, "
                f"COALESCE(c.title,'') AS chat_title "
                f"FROM incidents i "
                f"LEFT JOIN accounts a ON a.id=i.account_id "
                f"LEFT JOIN chats c ON c.id=i.chat_pk "
                f"WHERE i.status IN ({placeholders}) "
                f"ORDER BY CASE i.severity "
                f"  WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                f"  WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
                f"i.last_seen_at DESC LIMIT ?",
                (*statuses, limit),
            )
            return [_incident(r) for r in await cur.fetchall()]

    async def set_incident_status(self, incident_id: int, status: str) -> None:
        resolved = _now() if status == "resolved" else ""
        async with self._connect() as db:
            await db.execute(
                "UPDATE incidents SET status=?, resolved_at=? WHERE id=?",
                (status, resolved, incident_id),
            )
            await db.commit()

    async def resolve_incidents_matching(
        self,
        *,
        kind: str,
        account_id: int | None = None,
        chat_pk: int | None = None,
    ) -> int:
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE incidents SET status='resolved', resolved_at=? "
                "WHERE kind=? AND status IN ('open','fixing') "
                "AND IFNULL(account_id,-1)=IFNULL(?, -1) "
                "AND IFNULL(chat_pk,-1)=IFNULL(?, -1)",
                (_now(), kind, account_id, chat_pk),
            )
            await db.commit()
            return int(cur.rowcount or 0)

    async def add_health_snapshot(
        self,
        *,
        account_id: int,
        chat_pk: int,
        status: str,
        count: int,
        expected: int,
        error: str = "",
        sent_est: float = 0.0,
        checked_at: str | None = None,
    ) -> HealthSnapshot:
        ts = checked_at or _now()
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT INTO health_snapshots(account_id, chat_pk, checked_at, status, "
                "count, expected, error, sent_est) VALUES(?,?,?,?,?,?,?,?)",
                (
                    account_id,
                    chat_pk,
                    ts,
                    status,
                    int(count),
                    int(expected),
                    (error or "")[:500],
                    float(sent_est),
                ),
            )
            await db.commit()
            pk = int(cur.lastrowid)
        snap = await self.get_health_snapshot(pk)
        assert snap is not None
        return snap

    async def get_health_snapshot(self, snap_id: int) -> HealthSnapshot | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT h.*, COALESCE(a.label,'') AS account_label, "
                "COALESCE(c.title,'') AS chat_title "
                "FROM health_snapshots h "
                "LEFT JOIN accounts a ON a.id=h.account_id "
                "LEFT JOIN chats c ON c.id=h.chat_pk "
                "WHERE h.id=?",
                (snap_id,),
            )
            row = await cur.fetchone()
        return _health_snapshot(row) if row else None

    async def latest_health_snapshot(
        self, account_id: int, chat_pk: int
    ) -> HealthSnapshot | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT h.*, COALESCE(a.label,'') AS account_label, "
                "COALESCE(c.title,'') AS chat_title "
                "FROM health_snapshots h "
                "LEFT JOIN accounts a ON a.id=h.account_id "
                "LEFT JOIN chats c ON c.id=h.chat_pk "
                "WHERE h.account_id=? AND h.chat_pk=? "
                "ORDER BY h.id DESC LIMIT 1",
                (account_id, chat_pk),
            )
            row = await cur.fetchone()
        return _health_snapshot(row) if row else None

    async def add_send_stat(
        self,
        *,
        day: str,
        account_id: int,
        channel: str,
        messages: float,
        chat_pk: int | None = None,
    ) -> None:
        # SQLite UNIQUE treats NULLs as distinct тАФ use 0 for account-level rollups.
        pk = 0 if chat_pk is None else int(chat_pk)
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO send_stats_daily(day, account_id, channel, chat_pk, messages) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(day, account_id, channel, chat_pk) DO UPDATE SET "
                "messages = send_stats_daily.messages + excluded.messages",
                (day, account_id, channel, pk, float(messages)),
            )
            await db.commit()

    async def set_send_stat(
        self,
        *,
        day: str,
        account_id: int,
        channel: str,
        messages: float,
        chat_pk: int | None = None,
    ) -> None:
        """╨Р╨▒╤Б╨╛╨╗╤О╤В╨╜╨╛╨╡ ╨╖╨╜╨░╤З╨╡╨╜╨╕╨╡ ╨╖╨░ ╨┤╨╡╨╜╤М (╨┤╨╗╤П sender-╨╛╤Ж╨╡╨╜╨║╨╕, ╨▒╨╡╨╖ ╨╜╨░╨║╤А╤Г╤В╨║╨╕)."""
        pk = 0 if chat_pk is None else int(chat_pk)
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO send_stats_daily(day, account_id, channel, chat_pk, messages) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(day, account_id, channel, chat_pk) DO UPDATE SET "
                "messages = excluded.messages",
                (day, account_id, channel, pk, float(messages)),
            )
            await db.commit()

    async def send_stats_for_day(self, day: str) -> list[SendStatDay]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, COALESCE(a.label,'') AS account_label, "
                "COALESCE(c.title,'') AS chat_title "
                "FROM send_stats_daily s "
                "LEFT JOIN accounts a ON a.id=s.account_id "
                "LEFT JOIN chats c ON c.id=s.chat_pk "
                "WHERE s.day=? ORDER BY s.channel, a.label, c.title",
                (day,),
            )
            rows = await cur.fetchall()
        return [
            SendStatDay(
                day=r["day"],
                account_id=r["account_id"],
                channel=r["channel"],
                messages=float(r["messages"] or 0),
                chat_pk=r["chat_pk"],
                account_label=r["account_label"] or "",
                chat_title=r["chat_title"] or "",
            )
            for r in rows
        ]

    async def send_stats_since(self, day_from: str) -> list[SendStatDay]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, COALESCE(a.label,'') AS account_label, "
                "COALESCE(c.title,'') AS chat_title "
                "FROM send_stats_daily s "
                "LEFT JOIN accounts a ON a.id=s.account_id "
                "LEFT JOIN chats c ON c.id=s.chat_pk "
                "WHERE s.day >= ? ORDER BY s.day DESC, s.channel, a.label",
                (day_from,),
            )
            rows = await cur.fetchall()
        return [
            SendStatDay(
                day=r["day"],
                account_id=r["account_id"],
                channel=r["channel"],
                messages=float(r["messages"] or 0),
                chat_pk=r["chat_pk"],
                account_label=r["account_label"] or "",
                chat_title=r["chat_title"] or "",
            )
            for r in rows
        ]

    # тФАтФА leads тФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФА

    async def upsert_lead(
        self,
        *,
        account_id: int,
        user_id: int,
        username: str = "",
        seen_at: str | None = None,
        add_messages: int = 1,
    ) -> bool:
        """Return True if this is a newly created lead row."""
        ts = seen_at or _now()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id FROM leads WHERE account_id=? AND user_id=?",
                (account_id, user_id),
            )
            row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE leads SET username=CASE WHEN ?<>'' THEN ? ELSE username END, "
                    "last_seen_at=? WHERE id=?",
                    (username, username, ts, row["id"]),
                )
                await db.commit()
                return False
            await db.execute(
                "INSERT INTO leads(account_id, user_id, username, first_seen_at, "
                "last_seen_at, msg_count) VALUES(?,?,?,?,?,?)",
                (
                    account_id,
                    user_id,
                    username or "",
                    ts,
                    ts,
                    max(1, int(add_messages) if add_messages else 1),
                ),
            )
            await db.commit()
            return True

    async def get_lead(self, account_id: int, user_id: int) -> Lead | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT l.*, COALESCE(a.label,'') AS account_label "
                "FROM leads l LEFT JOIN accounts a ON a.id=l.account_id "
                "WHERE l.account_id=? AND l.user_id=?",
                (account_id, user_id),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return Lead(
            id=row["id"],
            account_id=row["account_id"],
            user_id=row["user_id"],
            username=row["username"] or "",
            first_seen_at=row["first_seen_at"] or "",
            last_seen_at=row["last_seen_at"] or "",
            msg_count=int(row["msg_count"] or 0),
            account_label=row["account_label"] or "",
        )

    async def count_leads(self, account_id: int | None = None) -> int:
        async with self._connect() as db:
            if account_id is None:
                cur = await db.execute("SELECT COUNT(*) FROM leads")
            else:
                cur = await db.execute(
                    "SELECT COUNT(*) FROM leads WHERE account_id=?", (account_id,)
                )
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def set_lead_stat(
        self,
        *,
        day: str,
        account_id: int,
        new_leads: int = 0,
        messages: int = 0,
    ) -> None:
        """╨Р╨▒╤Б╨╛╨╗╤О╤В╨╜╤Л╨╡ ╨╖╨╜╨░╤З╨╡╨╜╨╕╤П ╨╖╨░ ╨┤╨╡╨╜╤М (╨┐╨╡╤А╨╡╨╖╨░╨┐╨╕╤Б╤М ╨┐╨╛╤Б╨╗╨╡ ╤Б╨║╨░╨╜╨░)."""
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO lead_stats_daily(day, account_id, new_leads, messages) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(day, account_id) DO UPDATE SET "
                "new_leads=excluded.new_leads, messages=excluded.messages",
                (day, account_id, int(new_leads), int(messages)),
            )
            await db.commit()

    async def count_leads_since(self, account_id: int, since_iso_prefix: str) -> int:
        """╨Ы╨╕╨┤╤Л ╤Б first_seen_at ╨╜╨░╤З╨╕╨╜╨░╤П ╤Б since (╨╜╨░╨┐╤А╨╕╨╝╨╡╤А '2026-09-30')."""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM leads WHERE account_id=? AND first_seen_at >= ?",
                (account_id, since_iso_prefix),
            )
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def add_lead_stat(
        self,
        *,
        day: str,
        account_id: int,
        new_leads: int = 0,
        messages: int = 0,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO lead_stats_daily(day, account_id, new_leads, messages) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(day, account_id) DO UPDATE SET "
                "new_leads = lead_stats_daily.new_leads + excluded.new_leads, "
                "messages = lead_stats_daily.messages + excluded.messages",
                (day, account_id, int(new_leads), int(messages)),
            )
            await db.commit()

    async def lead_stats_for_day(self, day: str) -> list[LeadStatDay]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT s.*, COALESCE(a.label,'') AS account_label "
                "FROM lead_stats_daily s "
                "LEFT JOIN accounts a ON a.id=s.account_id "
                "WHERE s.day=? ORDER BY s.new_leads DESC, a.label",
                (day,),
            )
            rows = await cur.fetchall()
        return [
            LeadStatDay(
                day=r["day"],
                account_id=r["account_id"],
                new_leads=int(r["new_leads"] or 0),
                messages=int(r["messages"] or 0),
                account_label=r["account_label"] or "",
            )
            for r in rows
        ]

    async def save_density_day(
        self,
        day: str,
        rows: list[dict],
    ) -> None:
        async with self._connect() as db:
            for r in rows:
                await db.execute(
                    "INSERT INTO density_daily(day, chat_pk, accounts_live, avg_gap_min, "
                    "status, advice) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(day, chat_pk) DO UPDATE SET "
                    "accounts_live=excluded.accounts_live, avg_gap_min=excluded.avg_gap_min, "
                    "status=excluded.status, advice=excluded.advice",
                    (
                        day,
                        int(r["chat_pk"]),
                        int(r["accounts_live"]),
                        float(r["avg_gap_min"]),
                        r["status"],
                        (r.get("advice") or "")[:1000],
                    ),
                )
            await db.commit()

    async def density_for_day(self, day: str) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT d.*, COALESCE(c.title,'') AS chat_title "
                "FROM density_daily d "
                "LEFT JOIN chats c ON c.id=d.chat_pk "
                "WHERE d.day=? ORDER BY d.avg_gap_min DESC",
                (day,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def save_daily_report(self, day: str, payload: dict | str) -> DailyReport:
        import json

        raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        now = _now()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO daily_reports(day, created_at, updated_at, payload_json) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET updated_at=excluded.updated_at, "
                "payload_json=excluded.payload_json",
                (day, now, now, raw),
            )
            await db.commit()
        rep = await self.get_daily_report(day)
        assert rep is not None
        return rep

    async def get_daily_report(self, day: str) -> DailyReport | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM daily_reports WHERE day=?", (day,)
            )
            row = await cur.fetchone()
        if not row:
            return None
        return DailyReport(
            day=row["day"],
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            payload_json=row["payload_json"] or "{}",
        )

    async def list_report_days(self, limit: int = 30) -> list[str]:
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT day FROM daily_reports ORDER BY day DESC LIMIT ?",
                (limit,),
            )
            return [r[0] for r in await cur.fetchall()]

    # ---- люди, которые пишут аккаунтам (ЛС) ---------------------------------

    async def dm_known(self, account_id: int) -> dict[int, tuple[int, str]]:
        """user_id → (last_msg_id, last_msg_at) уже обработанных диалогов."""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT user_id, last_msg_id, last_msg_at FROM dm_people WHERE account_id=?",
                (account_id,),
            )
            return {int(r[0]): (int(r[1]), r[2] or "") for r in await cur.fetchall()}

    async def upsert_dm_person(
        self,
        account_id: int,
        user_id: int,
        *,
        username: str = "",
        name: str = "",
        started_by: str | None = None,
        first_at: str | None = None,
        last_in_at: str | None = None,
        last_msg_id: int = 0,
        last_msg_at: str = "",
        add_in: int = 0,
        unread: int = 0,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO dm_people(account_id, user_id, username, name, started_by, "
                "first_at, last_in_at, last_msg_id, last_msg_at, in_count, unread) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id, user_id) DO UPDATE SET "
                "username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE username END, "
                "name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE name END, "
                "started_by=CASE WHEN dm_people.started_by='unknown' "
                "  THEN excluded.started_by ELSE dm_people.started_by END, "
                "first_at=CASE WHEN dm_people.first_at='' THEN excluded.first_at "
                "  ELSE dm_people.first_at END, "
                "last_in_at=CASE WHEN excluded.last_in_at>dm_people.last_in_at "
                "  THEN excluded.last_in_at ELSE dm_people.last_in_at END, "
                "last_msg_id=MAX(dm_people.last_msg_id, excluded.last_msg_id), "
                "last_msg_at=CASE WHEN excluded.last_msg_at>dm_people.last_msg_at "
                "  THEN excluded.last_msg_at ELSE dm_people.last_msg_at END, "
                "in_count=dm_people.in_count+? , unread=excluded.unread",
                (
                    account_id,
                    user_id,
                    username,
                    name,
                    started_by or "unknown",
                    first_at or "",
                    last_in_at or "",
                    last_msg_id,
                    last_msg_at,
                    add_in,
                    unread,
                    add_in,
                ),
            )
            await db.commit()

    async def add_dm_activity(self, account_id: int, user_id: int, days: set[str]) -> None:
        if not days:
            return
        async with self._connect() as db:
            await db.executemany(
                "INSERT OR IGNORE INTO dm_activity(account_id, user_id, day) VALUES(?,?,?)",
                [(account_id, user_id, d) for d in days],
            )
            await db.commit()

    async def dm_summary(
        self, since_24h: str, since_7d: str
    ) -> dict[int, dict[str, int]]:
        """По каждому аккаунту: сколько людей написали (всего / первыми / за 24ч / 7д)."""
        out: dict[int, dict[str, int]] = {}
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT account_id, "
                "SUM(CASE WHEN in_count>0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN started_by='them' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN started_by='them' AND first_at>=? THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN started_by='them' AND first_at>=? THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN last_in_at>=? THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN unread>0 THEN 1 ELSE 0 END), "
                "COUNT(*) "
                "FROM dm_people GROUP BY account_id",
                (since_24h, since_7d, since_24h),
            )
            for r in await cur.fetchall():
                out[int(r[0])] = {
                    "wrote": int(r[1] or 0),
                    "first_total": int(r[2] or 0),
                    "first_24h": int(r[3] or 0),
                    "first_7d": int(r[4] or 0),
                    "active_24h": int(r[5] or 0),
                    "waiting": int(r[6] or 0),
                    "dialogs": int(r[7] or 0),
                }
        return out

    async def dm_active_on_day(self, day: str) -> dict[int, int]:
        """account_id → сколько разных людей писали в этот локальный день."""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT account_id, COUNT(*) FROM dm_activity WHERE day=? GROUP BY account_id",
                (day,),
            )
            return {int(r[0]): int(r[1]) for r in await cur.fetchall()}

    async def dm_new_on_day(self, day_start_utc: str, day_end_utc: str) -> dict[int, int]:
        """account_id → сколько людей написали впервые в этот день."""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT account_id, COUNT(*) FROM dm_people "
                "WHERE started_by='them' AND first_at>=? AND first_at<? GROUP BY account_id",
                (day_start_utc, day_end_utc),
            )
            return {int(r[0]): int(r[1]) for r in await cur.fetchall()}

    async def resolve_incidents_by_source(self, source: str) -> int:
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE incidents SET status='resolved', resolved_at=? "
                "WHERE source=? AND status IN ('open','fixing')",
                (_now(), source),
            )
            await db.commit()
            return int(cur.rowcount or 0)

    async def resolve_incidents_not_in(
        self, source: str, keep: set[tuple[str, int | None, int | None]]
    ) -> int:
        """Закрыть открытые инциденты источника, которых нет в актуальном списке."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, kind, account_id, chat_pk FROM incidents "
                "WHERE source=? AND status IN ('open','fixing')",
                (source,),
            )
            rows = await cur.fetchall()
            stale = [
                r["id"]
                for r in rows
                if (r["kind"], r["account_id"], r["chat_pk"]) not in keep
            ]
            for pk in stale:
                await db.execute(
                    "UPDATE incidents SET status='resolved', resolved_at=? WHERE id=?",
                    (_now(), pk),
                )
            await db.commit()
            return len(stale)

    async def first_send_at(self) -> str:
        async with self._connect() as db:
            cur = await db.execute("SELECT MIN(sent_at) FROM send_events")
            row = await cur.fetchone()
        return (row[0] if row and row[0] else "") or ""

    async def resolve_restrictions_kind(self, account_id: int, kind: str) -> list[int]:
        """Снять все активные ограничения вида kind у аккаунта; вернуть chat_pk."""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT chat_pk FROM chat_restrictions WHERE account_id=? AND kind=? AND active=1",
                (account_id, kind),
            )
            pks = [int(r[0]) for r in await cur.fetchall()]
            if pks:
                await db.execute(
                    "UPDATE chat_restrictions SET active=0, resolved_at=? "
                    "WHERE account_id=? AND kind=? AND active=1",
                    (_now(), account_id, kind),
                )
                await db.execute(
                    "UPDATE setup_states SET status='pending', fail_count=0, last_error='' "
                    "WHERE account_id=? AND status<>'ok' AND chat_pk IN (%s)"
                    % ",".join("?" for _ in pks),
                    (account_id, *pks),
                )
                await db.commit()
            return pks

    async def list_restrictions_expired(self, now_iso: str) -> list[Restriction]:
        """Активные мут/spamblock, у которых срок вышел (пора перепроверить)."""
        rows = await self.list_restrictions(kinds=("mute", "nowrite", "spamblock"))
        return [r for r in rows if r.until_at and r.until_at <= now_iso]
