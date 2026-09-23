from app.models import Chat, Post
from app.utils.templates import (
    SHORT_POST_MAX_CHARS,
    pick_post_for_chat,
    short_lang,
)


def test_short_lang():
    assert short_lang("ru") == "ru_short"
    assert short_lang("EN") == "en_short"
    assert short_lang("ru_short") == "ru_short"


def test_pick_full_by_default():
    posts = {
        "ru": Post(lang="ru", text="full-ru"),
        "ru_short": Post(lang="ru_short", text="short-ru"),
    }
    chat = Chat(id=1, title="A", chat_id="-1", lang="ru", text_kind="full")
    assert pick_post_for_chat(posts, chat).text == "full-ru"


def test_pick_short_when_enabled():
    posts = {
        "ru": Post(lang="ru", text="full-ru"),
        "en": Post(lang="en", text="full-en"),
        "ru_short": Post(lang="ru_short", text="short-ru"),
        "en_short": Post(lang="en_short", text="short-en"),
    }
    chat = Chat(id=1, title="A", chat_id="-1", lang="en", text_kind="short")
    assert pick_post_for_chat(posts, chat).text == "short-en"


def test_pick_short_falls_back_to_full():
    posts = {
        "ru": Post(lang="ru", text="full-ru"),
        "ru_short": Post(lang="ru_short", text=""),
    }
    chat = Chat(id=1, title="A", chat_id="-1", lang="ru", text_kind="short")
    assert pick_post_for_chat(posts, chat).text == "full-ru"


def test_short_max_chars_constant():
    assert SHORT_POST_MAX_CHARS == 500
