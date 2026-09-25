from app.models import Post
from app.utils.tg_links import format_message_link, parse_message_link


def test_parse_public_link():
    p = parse_message_link("https://t.me/MyChannel/42")
    assert p is not None
    assert p["chat"] == "MyChannel"
    assert p["msg_id"] == 42


def test_parse_private_c_link():
    p = parse_message_link("https://t.me/c/1234567890/99?single")
    assert p is not None
    assert p["chat"] == -1001234567890
    assert p["msg_id"] == 99


def test_parse_invalid():
    assert parse_message_link("https://t.me/joinchat/xxx") is None
    assert parse_message_link("") is None


def test_format_link():
    assert "t.me/c/" in format_message_link(-1001234567890, 5)
    assert format_message_link("chan", 3).endswith("/3")


def test_pick_forward_prefers_photo_when_wanted():
    p = Post(
        lang="ru",
        delivery="link",
        link_chat_id="@a",
        link_msg_id=1,
        link_photo_chat_id="@b",
        link_photo_msg_id=2,
    )
    assert p.pick_forward(want_photo=True) == ("b", 2)
    assert p.pick_forward(want_photo=False) == ("a", 1)


def test_pick_forward_fallback_to_text():
    p = Post(lang="ru", delivery="link", link_chat_id="-1001", link_msg_id=7)
    assert p.pick_forward(want_photo=True) == (-1001, 7)
