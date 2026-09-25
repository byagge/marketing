from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Account:
    id: int
    label: str
    phone: str = ""
    username: str = ""
    user_id: int | None = None
    telethon_session: str = ""
    pyrogram_session: str = ""
    sender_bot_token: str = ""
    sender_account_id: str = ""
    is_premium: int = 0
    online_ping: int = 1
    status: str = "idle"
    last_error: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def display(self) -> str:
        uname = f"@{self.username}" if self.username else (self.phone or f"id={self.id}")
        return f"{self.label} ({uname})"

    @property
    def has_telethon(self) -> bool:
        return bool(self.telethon_session)

    @property
    def has_premium(self) -> bool:
        return bool(self.is_premium)

    @property
    def online_ping_enabled(self) -> bool:
        return bool(self.online_ping)

    @property
    def has_sender(self) -> bool:
        if (self.sender_account_id or "").strip():
            return True
        return bool(self.pyrogram_session) and bool((self.sender_bot_token or "").strip())


@dataclass
class Post:
    lang: str
    text: str = ""
    entities_json: str = "[]"
    photo_path: str = ""
    account_id: int = 0
    # post = свой текст/фото; link = пересылка из канала (сохраняет Forwarded from)
    delivery: str = "post"
    link_url: str = ""
    link_chat_id: str = ""  # int as str or @username
    link_msg_id: int = 0
    link_photo_url: str = ""
    link_photo_chat_id: str = ""
    link_photo_msg_id: int = 0

    @property
    def is_short(self) -> bool:
        return (self.lang or "").endswith("_short")

    @property
    def is_link_mode(self) -> bool:
        return (self.delivery or "post") == "link"

    @property
    def has_compose(self) -> bool:
        return bool((self.text or "").strip() or (self.photo_path or "").strip())

    @property
    def has_text_link(self) -> bool:
        return bool(self.link_msg_id and (self.link_chat_id or "").strip())

    @property
    def has_photo_link(self) -> bool:
        return bool(self.link_photo_msg_id and (self.link_photo_chat_id or "").strip())

    def pick_forward(
        self, *, want_photo: bool
    ) -> tuple[str | int, int] | None:
        """Выбрать источник пересылки: фото-ссылка или текстовая."""
        if want_photo and self.has_photo_link:
            return _peer_ref(self.link_photo_chat_id), int(self.link_photo_msg_id)
        if self.has_text_link:
            return _peer_ref(self.link_chat_id), int(self.link_msg_id)
        if self.has_photo_link:
            return _peer_ref(self.link_photo_chat_id), int(self.link_photo_msg_id)
        return None


def _peer_ref(raw: str) -> str | int:
    s = (raw or "").strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return s.lstrip("@")


@dataclass
class Chat:
    id: int
    title: str
    chat_id: str
    username: str = ""
    kind: str = "sender"  # schedule | sender
    lang: str = "ru"
    tag: str = ""
    interval_minutes: int = 60
    enabled: int = 1
    invite_link: str = ""
    captcha_kind: str = "auto"  # auto | none | poll | verify_btn | math_btn
    join_mode: str = "direct"  # direct | garant | request
    garant_bot: str = ""
    after_join: str = "none"  # none | confirm | bot_captcha
    after_join_bot: str = ""
    require_channels: str = ""  # @ch1, @ch2 или ссылки
    is_join_request: int = 0
    text_kind: str = "full"  # full | short
    allow_media: int = 1  # 0 = чат без фото, шлём текст / text-link
    created_at: str = ""

    @property
    def is_schedule(self) -> bool:
        return self.kind == "schedule"

    @property
    def uses_short_text(self) -> bool:
        return (self.text_kind or "full") == "short"

    @property
    def media_allowed(self) -> bool:
        return bool(self.allow_media)

    @property
    def tg_id(self) -> int | str:
        raw = self.chat_id.strip()
        if raw.lstrip("-").isdigit():
            return int(raw)
        return raw

    @property
    def display_name(self) -> str:
        title = (self.title or "").strip()
        if title and not _looks_like_tg_id(title):
            return title
        if self.username:
            return self.username
        return title or self.chat_id or "чат"

    @property
    def has_join_link(self) -> bool:
        return bool((self.invite_link or self.username or "").strip())

    @property
    def join_request(self) -> bool:
        return bool(self.is_join_request)


def _looks_like_tg_id(value: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return True
    if raw.startswith("@"):
        return False
    return raw.lstrip("-").isdigit()


@dataclass
class SetupState:
    """Per account×chat schedule setup progress for missing/unavailable chats."""

    id: int
    account_id: int
    chat_pk: int
    status: str = "pending"  # ok | pending | abandoned
    fail_count: int = 0
    last_attempt_at: str = ""
    last_error: str = ""
    account_label: str = ""
    chat_title: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def is_abandoned(self) -> bool:
        return self.status == "abandoned"


@dataclass
class MinuteSlot:
    id: int
    chat_pk: int
    account_id: int
    start_minute: int
    account_label: str = ""
    chat_title: str = ""


@dataclass
class Job:
    id: int
    account_id: int | None
    kind: str
    status: str
    started_at: str = ""
    finished_at: str = ""
    report: str = ""
    account_label: str = ""


@dataclass
class JobLog:
    id: int
    job_id: int
    created_at: str
    level: str
    message: str


@dataclass
class SenderSettings:
    between_min: int = 30
    between_max: int = 90
    cycle_min: int = 300
    cycle_max: int = 600
    per_chat_min: int = 3600
    per_chat_max: int = 3600
    parallel: int = 4
    cloak_enabled: bool = False
    cloak_text: str = ""
    cloak_entities_json: str = "[]"
    mentions_enabled: bool = False  # глобальные отметки; по умолчанию выкл
    keep_extra_ids: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class OnlinePingSettings:
    enabled: bool = True
    hours: float = 3.5
    jitter_sec: int = 1800
    hold_seconds: float = 4.0
    next_at: str = ""
    last_at: str = ""
