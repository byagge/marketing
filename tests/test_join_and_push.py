from app.models import Chat
from app.tg.join import (
    _entity_from_join_result,
    chat_join_target,
    extract_invite_hash,
    extract_public_username,
    needs_bot_flow,
)
from app.tg.sender_push import has_premium_emoji, normalize_multiline


def test_invite_hash():
    assert extract_invite_hash("https://t.me/+AbCdEf123") == "AbCdEf123"
    assert extract_invite_hash("t.me/joinchat/XXXXYY") == "XXXXYY"


def test_public_username():
    assert extract_public_username("https://t.me/MyChat") == "MyChat"
    assert extract_public_username("@mychat") == "mychat"


def test_bot_flow():
    assert needs_bot_flow("https://t.me/SomeBot?start=abc")
    assert not needs_bot_flow("https://t.me/+hash")


def test_chat_join_target_invite():
    chat = Chat(id=1, title="A", chat_id="-1001", invite_link="https://t.me/+xyz")
    t = chat_join_target(chat)
    assert t["method"] == "invite"
    assert t["value"] == "xyz"


def test_entity_from_join_empty_chats():
    class Empty:
        chats = []

    assert _entity_from_join_result(Empty()) is None

    class WithChat:
        chat = "entity"

    assert _entity_from_join_result(WithChat()) == "entity"

    class WithChats:
        chats = ["a", "b"]

    assert _entity_from_join_result(WithChats()) == "a"


def test_normalize_keeps_newlines():
    text = "строка1\n\nстрока2\n"
    assert normalize_multiline(text) == "строка1\n\nстрока2"


def test_premium_emoji_detect():
    assert has_premium_emoji(
        [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "1"}]
    )
    assert not has_premium_emoji([{"type": "bold", "offset": 0, "length": 2}])
