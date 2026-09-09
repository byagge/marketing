from app.models import Chat


def test_display_name_prefers_title():
    chat = Chat(id=1, title="Lustify", chat_id="-100123")
    assert chat.display_name == "Lustify"


def test_display_name_hides_numeric_title():
    chat = Chat(id=1, title="-100123", chat_id="-100123", username="lustify")
    assert chat.display_name == "lustify"


def test_display_name_falls_back_to_id():
    chat = Chat(id=1, title="-100123", chat_id="-100123")
    assert chat.display_name == "-100123"
