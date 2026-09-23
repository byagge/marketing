from app.models import Chat
from app.tg.join_flows import parse_folder_slug, split_channels
from app.tg.join_presets import effective_join_config, match_preset
from app.tg.join import chat_join_target, sort_chats_for_join
from app.utils.captcha import pick_verify_button, solve_math_captcha


def test_lustify_preset():
    chat = Chat(id=1, title="Lustify Chat", chat_id="-1002499948325")
    p = match_preset(chat)
    assert p is not None
    assert p["garant_bot"] == "LustifyGarant_bot"
    assert p["after_join"] == "confirm"
    cfg = effective_join_config(chat)
    assert cfg["join_mode"] == "garant"
    assert chat_join_target(chat)["method"] == "garant"


def test_lsa_preset():
    chat = Chat(id=2, title="LSA", chat_id="-1002415711408")
    cfg = effective_join_config(chat)
    assert cfg["garant_bot"] == "GUARD_LSA_BOT"
    assert cfg["after_join"] == "bot_captcha"
    assert cfg["after_join_bot"] == "LSA_GRNT_BOT"


def test_market404_channels():
    chat = Chat(
        id=3,
        title="MARKET 404 | УСЛУГИ",
        chat_id="-1001",
        invite_link="https://t.me/+H3mT0QmCnltmYjY6",
    )
    cfg = effective_join_config(chat)
    assert "market404chat" in cfg["require_channels"]


def test_folder_slug():
    assert parse_folder_slug("https://t.me/addlist/AbCd123") == "AbCd123"
    assert split_channels("@a, @b") == ["@a", "@b"]


def test_sort_requests_last():
    a = Chat(id=1, title="A", chat_id="1", is_join_request=0)
    b = Chat(id=2, title="B", chat_id="2", is_join_request=1, join_mode="request")
    ordered = sort_chats_for_join([b, a])
    assert ordered[0].id == 1
    assert ordered[-1].id == 2


def test_math_buttons_with_refresh():
    from types import SimpleNamespace

    buttons = [
        SimpleNamespace(text="5", data=b"1"),
        SimpleNamespace(text="4", data=b"2"),
        SimpleNamespace(text="3", data=b"3"),
        SimpleNamespace(text="2", data=b"4"),
        SimpleNamespace(text="🔄 Обновить", data=b"r"),
    ]
    # 2 rows of 2 + refresh row
    rows = [
        SimpleNamespace(buttons=buttons[:2]),
        SimpleNamespace(buttons=buttons[2:4]),
        SimpleNamespace(buttons=buttons[4:]),
    ]
    msg = SimpleNamespace(
        message="🤖 Проверка на человека\nСколько будет 1 + 1?",
        reply_markup=SimpleNamespace(rows=rows),
    )
    assert solve_math_captcha(msg.message) == "2"
    picked = pick_verify_button(msg)
    assert picked is not None
    assert picked[2] == "2"
