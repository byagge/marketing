from app.models import Chat
from app.tg.join import (
    _entity_from_join_result,
    chat_join_target,
    extract_invite_hash,
    extract_public_username,
    needs_bot_flow,
)
from app.tg.sender_push import (
    apply_mentions_everywhere,
    disable_mentions_everywhere,
    has_premium_emoji,
    normalize_multiline,
    push_chat_text,
    push_cloak,
)

import pytest


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


@pytest.mark.asyncio
async def test_push_cloak_verifies_and_retries():
    class FakeAPI:
        def __init__(self):
            self.puts = []
            self.state = {"enabled": False, "text": ""}

        async def put_cloak(self, account_id, enabled, text, **kwargs):
            self.puts.append((account_id, enabled, text))
            self.state = {"enabled": bool(enabled), "text": text}

        async def get_cloak(self, account_id):
            return dict(self.state)

    api = FakeAPI()
    res = await push_cloak(api, "acc1", enabled=True, text="привет\nмир")
    assert res["ok"] is True
    assert res["enabled"] is True
    assert api.puts[0] == ("acc1", True, "привет\nмир")


@pytest.mark.asyncio
async def test_disable_mentions_everywhere():
    class FakeAPI:
        def __init__(self):
            self.mentions = None
            self.patches = []

        async def put_mentions(self, account_id, enabled):
            self.mentions = enabled

        async def list_chats(self, account_id):
            return [{"chat_id": "-1001"}, {"chat_id": "-1002"}]

        async def patch_chat(self, account_id, chat_id, **fields):
            self.patches.append((chat_id, fields))

    api = FakeAPI()
    n = await disable_mentions_everywhere(api, "acc1")
    assert api.mentions is False
    assert n == 2
    assert all(p[1].get("mention") is False for p in api.patches)


@pytest.mark.asyncio
async def test_apply_mentions_everywhere_on():
    class FakeAPI:
        def __init__(self):
            self.mentions = None
            self.patches = []

        async def put_mentions(self, account_id, enabled):
            self.mentions = enabled

        async def list_chats(self, account_id):
            return [{"chat_id": "-1001"}]

        async def patch_chat(self, account_id, chat_id, **fields):
            self.patches.append((chat_id, fields))

    api = FakeAPI()
    n = await apply_mentions_everywhere(api, "acc1", enabled=True)
    assert api.mentions is True
    assert n == 1
    assert api.patches[0][1].get("mention") == "global"


@pytest.mark.asyncio
async def test_push_chat_text_forces_mention_off():
    class FakeAPI:
        def __init__(self):
            self.fields = None

        async def patch_chat(self, account_id, chat_id, **fields):
            self.fields = fields
            return {"ok": True}

    api = FakeAPI()
    await push_chat_text(api, "acc1", "-1001", "hi\nthere")
    assert api.fields["mention"] is False
    assert api.fields["active"] is True
    assert api.fields["mode"] == "post"
    assert api.fields["text"] == "hi\nthere"


@pytest.mark.asyncio
async def test_push_chat_text_mentions_on():
    class FakeAPI:
        def __init__(self):
            self.fields = None

        async def patch_chat(self, account_id, chat_id, **fields):
            self.fields = fields
            return {"ok": True}

    api = FakeAPI()
    await push_chat_text(api, "acc1", "-1001", "hi", mentions_enabled=True)
    assert api.fields["mention"] == "global"
