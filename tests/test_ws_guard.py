from types import SimpleNamespace

from app.models import Chat
from app.tg.join_presets import effective_join_config, match_preset
from app.tg.ws_guard import (
    _label_match,
    _parse_webapp_init,
    extract_invite_from_message,
    is_ws_guard_bot,
)


def test_is_ws_guard_bot():
    assert is_ws_guard_bot("WsGuardBot")
    assert is_ws_guard_bot("@wsguardbot")
    assert is_ws_guard_bot("WS_Guard_Bot")
    assert not is_ws_guard_bot("LustifyGarant_bot")
    assert not is_ws_guard_bot("")


def test_ws_guard_preset_by_garant():
    chat = Chat(id=1, title="Some chat", chat_id="-100999", garant_bot="WsGuardBot")
    p = match_preset(chat)
    assert p is not None
    assert p.get("garant_flow") == "ws_guard"
    cfg = effective_join_config(chat)
    assert cfg["garant_bot"].lower() == "wsguardbot"
    assert cfg["garant_flow"] == "ws_guard"
    assert cfg["join_mode"] == "garant"


def test_ws_guard_preset_by_title():
    chat = Chat(id=2, title="WS GUARD chat", chat_id="-100998")
    p = match_preset(chat)
    assert p is not None
    assert p["garant_bot"] == "WsGuardBot"


def test_parse_webapp_init():
    url = (
        "https://example.com/terms#tgWebAppData=user%3D%257B%257D%26auth_date%3D1"
        "&tgWebAppVersion=7.0"
    )
    origin, data = _parse_webapp_init(url)
    assert origin == "https://example.com"
    assert "auth_date" in data or data.startswith("user")


def test_label_match_terms_and_help():
    assert _label_match("📋 Условия пользования", ("услови", "terms"))
    assert _label_match("❔ Помощь", ("помощь", "help"))
    assert _label_match("🔗 Получить ссылку на чат", ("получить ссылку", "ссылку на чат"))
    assert _label_match("🔗 Перейти в канал", ("перейти в канал", "join"))
    assert _label_match("Запросить ссылку", ("запросить ссылку",))


def test_extract_invite_prefers_open_channel_button():
    open_btn = SimpleNamespace(text="🔗 Перейти в канал", url="https://t.me/+AbCdEfGhIjK")
    other = SimpleNamespace(text="ignore", url="https://example.com")
    row = SimpleNamespace(buttons=[other, open_btn])
    markup = SimpleNamespace(rows=[row])
    msg = SimpleNamespace(reply_markup=markup, message="", entities=None)
    assert extract_invite_from_message(msg) == "https://t.me/+AbCdEfGhIjK"


def test_extract_invite_from_get_link_without_url_returns_none():
    btn = SimpleNamespace(text="🔗 Получить ссылку на чат", url=None, web_app=None)
    row = SimpleNamespace(buttons=[btn])
    markup = SimpleNamespace(rows=[row])
    msg = SimpleNamespace(
        reply_markup=markup,
        message="Одноразовую ссылку на чат вы можете получить по кнопке ниже!",
        entities=None,
    )
    assert extract_invite_from_message(msg) is None
