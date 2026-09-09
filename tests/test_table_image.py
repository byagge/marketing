from app.models import Account, Chat, MinuteSlot
from app.ui.table_image import render_chat_png, render_overview_png, try_overview_png


def test_overview_png_empty():
    data = render_overview_png([], [], [])
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 200


def test_overview_png_with_slots():
    acc = Account(id=1, label="alpha")
    chat = Chat(id=2, title="Casino", chat_id="-1001", kind="schedule", interval_minutes=60)
    slot = MinuteSlot(id=1, chat_pk=2, account_id=1, start_minute=0, account_label="alpha")
    data = render_overview_png([chat], [acc], [slot])
    assert data.startswith(b"\x89PNG")


def test_chat_png():
    acc = Account(id=1, label="alpha")
    chat = Chat(id=2, title="Casino", chat_id="-1001", kind="schedule", interval_minutes=60)
    slot = MinuteSlot(id=1, chat_pk=2, account_id=1, start_minute=30, account_label="alpha")
    data = render_chat_png(chat, [slot], [acc])
    assert data.startswith(b"\x89PNG")


def test_try_overview_never_raises():
    assert try_overview_png([], [], []) is not None
