"""Пресеты вступления для известных чатов (без ручной привязки при создании).

Срабатывают по chat_id / @username / куску названия.
В карточке чата поля можно переопределить вручную.
"""

from __future__ import annotations

from typing import Any

from app.models import Chat

# captcha_kind: auto | none | poll | verify_btn | math_btn
# join_mode:    direct | garant | request
# after_join:   none | confirm | bot_captcha

PRESETS: list[dict[str, Any]] = [
    {
        "ids": {"-1002499948325", "1002499948325"},
        "titles": ("lustify",),
        "join_mode": "garant",
        "garant_bot": "LustifyGarant_bot",
        "after_join": "confirm",
        "after_join_bot": "LustifyGarant_bot",
        "captcha_kind": "auto",
        "require_channels": "",
        "is_join_request": 0,
        "garant_flow": "lustify",
    },
    {
        "ids": {"-1002415711408", "1002415711408"},
        "titles": ("lsa",),
        "usernames": (),
        "join_mode": "garant",
        "garant_bot": "GUARD_LSA_BOT",
        "after_join": "bot_captcha",
        "after_join_bot": "LSA_GRNT_BOT",
        "captcha_kind": "math_btn",
        "require_channels": "",
        "is_join_request": 0,
        "garant_flow": "lsa",
    },
    {
        "ids": set(),
        "titles": ("ws guard", "wsguard", "ws garant", "wsgarant"),
        "usernames": ("wsguardbot",),
        "garant_bots": ("WsGuardBot",),
        "join_mode": "garant",
        "garant_bot": "WsGuardBot",
        "after_join": "none",
        "after_join_bot": "",
        "captcha_kind": "auto",
        "require_channels": "",
        "is_join_request": 0,
        "garant_flow": "ws_guard",
    },
    {
        "ids": set(),
        "titles": ("market 404", "market404"),
        "invite_hashes": {"H3mT0QmCnltmYjY6"},
        "join_mode": "direct",
        "garant_bot": "",
        "after_join": "none",
        "after_join_bot": "",
        "captcha_kind": "auto",
        "require_channels": "market404chat",
        "is_join_request": 0,
        "garant_flow": "",
    },
]

CAPTCHA_KIND_CYCLE = ("auto", "none", "poll", "verify_btn", "math_btn")
CAPTCHA_KIND_LABEL = {
    "auto": "авто",
    "none": "без капчи",
    "poll": "quiz-опрос",
    "verify_btn": "кнопка «не бот»",
    "math_btn": "кнопки-числа",
}
JOIN_MODE_CYCLE = ("direct", "garant", "request")
JOIN_MODE_LABEL = {
    "direct": "ссылка/@",
    "garant": "через бота",
    "request": "заявка",
}
AFTER_JOIN_CYCLE = ("none", "confirm", "bot_captcha")
AFTER_JOIN_LABEL = {
    "none": "нет",
    "confirm": "«Я вступил»",
    "bot_captcha": "капча в боте",
}


def _norm_id(chat_id: str) -> str:
    raw = (chat_id or "").strip()
    if raw.isdigit():
        return f"-{raw}" if not raw.startswith("-") else raw
    return raw


def match_preset(chat: Chat | None = None, **hints: str) -> dict[str, Any] | None:
    chat_id = _norm_id(hints.get("chat_id") or (chat.chat_id if chat else "") or "")
    title = (hints.get("title") or (chat.title if chat else "") or "").casefold()
    username = (hints.get("username") or (chat.username if chat else "") or "").casefold().lstrip("@")
    invite = hints.get("invite_link") or (chat.invite_link if chat else "") or ""
    garant = (
        hints.get("garant_bot") or (chat.garant_bot if chat else "") or ""
    ).casefold().lstrip("@")

    for preset in PRESETS:
        ids = {_norm_id(x) for x in preset.get("ids") or set()}
        if chat_id and chat_id in ids:
            return preset
        for frag in preset.get("titles") or ():
            if frag and frag in title:
                return preset
        for uname in preset.get("usernames") or ():
            if uname and uname.casefold().lstrip("@") == username:
                return preset
        for gb in preset.get("garant_bots") or ():
            if gb and gb.casefold().lstrip("@") == garant:
                return preset
        # чат с уже прописанным WsGuardBot
        if garant and preset.get("garant_flow") == "ws_guard":
            from app.tg.ws_guard import is_ws_guard_bot

            if is_ws_guard_bot(garant):
                return preset
        for h in preset.get("invite_hashes") or ():
            if h and h in invite:
                return preset
    return None


def apply_preset_fields(chat: Chat) -> dict[str, Any]:
    """Поля пресета только если у чата ещё дефолты (не перетираем ручные настройки)."""
    preset = match_preset(chat)
    if not preset:
        return {}
    fields: dict[str, Any] = {}
    # join_mode default direct — ставим garant из пресета
    if (chat.join_mode or "direct") == "direct" and preset.get("join_mode"):
        fields["join_mode"] = preset["join_mode"]
    if not (chat.garant_bot or "").strip() and preset.get("garant_bot"):
        fields["garant_bot"] = preset["garant_bot"]
    if (chat.after_join or "none") == "none" and preset.get("after_join"):
        fields["after_join"] = preset["after_join"]
    if not (chat.after_join_bot or "").strip() and preset.get("after_join_bot"):
        fields["after_join_bot"] = preset["after_join_bot"]
    if (chat.captcha_kind or "auto") == "auto" and preset.get("captcha_kind"):
        fields["captcha_kind"] = preset["captcha_kind"]
    if not (chat.require_channels or "").strip() and preset.get("require_channels"):
        fields["require_channels"] = preset["require_channels"]
    if not chat.is_join_request and preset.get("is_join_request"):
        fields["is_join_request"] = int(preset["is_join_request"])
    return fields


def effective_join_config(chat: Chat) -> dict[str, Any]:
    """Итоговые настройки: ручные поля чата + пресет как fallback для дефолтов."""
    preset = match_preset(chat) or {}

    join_mode = chat.join_mode or "direct"
    if join_mode == "direct" and preset.get("join_mode"):
        join_mode = preset["join_mode"]

    after_join = chat.after_join or "none"
    if after_join == "none" and preset.get("after_join"):
        after_join = preset["after_join"]

    captcha_kind = chat.captcha_kind or "auto"
    if captcha_kind == "auto" and preset.get("captcha_kind"):
        captcha_kind = preset["captcha_kind"]

    return {
        "join_mode": join_mode,
        "garant_bot": (chat.garant_bot or preset.get("garant_bot") or "").lstrip("@"),
        "after_join": after_join,
        "after_join_bot": (
            chat.after_join_bot
            or preset.get("after_join_bot")
            or chat.garant_bot
            or preset.get("garant_bot")
            or ""
        ).lstrip("@"),
        "captcha_kind": captcha_kind,
        "require_channels": (chat.require_channels or preset.get("require_channels") or ""),
        "is_join_request": bool(chat.is_join_request or preset.get("is_join_request")),
        "invite_link": (chat.invite_link or "").strip(),
        "preset": bool(preset),
        "garant_flow": preset.get("garant_flow") or "",
    }
