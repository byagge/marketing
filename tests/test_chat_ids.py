from app.jobs.setup import match_catalog_chat, pick_sender_live_chats
from app.models import Chat
from app.utils.chat_ids import chat_id_aliases, chat_ids_match


def _chat(pk: int, cid: str, kind: str = "schedule") -> Chat:
    return Chat(id=pk, title=f"c{pk}", chat_id=cid, kind=kind)


def test_aliases_super_group():
    keys = chat_id_aliases("-100123")
    assert "-100123" in keys
    assert "123" in keys
    assert chat_ids_match("-100123", "123")
    assert chat_ids_match(-100123, 123)


def test_aliases_plain():
    assert chat_ids_match("99", "-10099")
    assert not chat_ids_match("99", "88")
    assert not chat_ids_match("", "99")


def test_pick_excludes_schedule_and_aliases():
    schedule = [_chat(1, "-10099")]
    live = [
        {"chat_id": "-10099", "title": "schedule"},
        {"chat_id": "99", "title": "same schedule alias"},
        {"chat_id": "-10088", "title": "other"},
        {"chat_id": "", "title": "empty"},
    ]
    picked = pick_sender_live_chats(live, schedule)
    assert [c["chat_id"] for c in picked] == ["-10088"]


def test_catalog_sender_match():
    sender = [_chat(2, "88", "sender")]
    assert match_catalog_chat("-10088", sender) is sender[0]
    assert match_catalog_chat("-10077", sender) is None
