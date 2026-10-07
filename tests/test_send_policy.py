from datetime import datetime, timedelta, timezone

from app.models import Account, AccountChatPref, Chat, JoinState
from app.utils.chat_search import PAGE_SIZE, clamp_page, filter_chats, page_count, page_slice
from app.utils.send_policy import (
    REASON_BANNED,
    REASON_CHAT_OFF,
    REASON_PAIR_OFF,
    REASON_SENDER_OFF,
    classify_failure,
    effective_interval_minutes,
    effective_interval_seconds,
    is_send_allowed,
    join_due,
    pick_text_override,
    removal_confirmed,
)


def _chat(**kw) -> Chat:
    base = dict(id=1, title="Chat", chat_id="-100", kind="sender", interval_minutes=60, enabled=1)
    base.update(kw)
    return Chat(**base)


def _acc(**kw) -> Account:
    return Account(id=1, label="a", **kw)


def test_allowed_by_default():
    assert is_send_allowed(_acc(), _chat(), None, False) == (True, "")


def test_pair_off_and_ban_block_both_kinds():
    off = AccountChatPref(account_id=1, chat_pk=1, send_enabled=0)
    for kind in ("sender", "schedule"):
        assert is_send_allowed(_acc(), _chat(kind=kind), off, False) == (False, REASON_PAIR_OFF)
        assert is_send_allowed(_acc(), _chat(kind=kind), None, True) == (False, REASON_BANNED)


def test_sender_toggle_affects_only_sender_chats():
    acc = _acc(sender_enabled=0)
    assert is_send_allowed(acc, _chat(kind="sender"), None, False) == (False, REASON_SENDER_OFF)
    assert is_send_allowed(acc, _chat(kind="schedule"), None, False) == (True, "")


def test_disabled_chat_blocks():
    assert is_send_allowed(_acc(), _chat(enabled=0), None, False) == (False, REASON_CHAT_OFF)


def test_effective_interval_applies_daily_limit():
    assert effective_interval_minutes(_chat(interval_minutes=60)) == 60
    # 3 поста в сутки → не чаще раза в 8 часов
    assert effective_interval_minutes(_chat(interval_minutes=60, max_posts_per_account=3)) == 480
    assert effective_interval_seconds(_chat(interval_minutes=60, max_posts_per_account=3)) == 28800
    # интервал уже реже лимита — остаётся свой
    assert effective_interval_minutes(_chat(interval_minutes=720, max_posts_per_account=3)) == 720
    # не делится нацело → округляем вверх, лимит не превышаем
    assert effective_interval_minutes(_chat(interval_minutes=10, max_posts_per_account=7)) == 206


def test_text_override():
    assert pick_text_override(None) is None
    assert pick_text_override(AccountChatPref(1, 1, text="  ")) is None
    assert pick_text_override(AccountChatPref(1, 1, text="hi")) == "hi"


def test_classify_failure():
    assert classify_failure("UserBannedInChannelError: You're banned from sending") == "ban"
    assert classify_failure("FloodWait 30s") == "flood"
    assert classify_failure("FloodWaitError: wait") == "flood"
    assert classify_failure("инвайт недействителен: InviteHashExpiredError") == "bad_invite"
    assert classify_failure("ChatWriteForbiddenError: no rights") == "muted"
    assert classify_failure("TimeoutError") == "other"
    assert classify_failure("") == "other"


def _state(**kw) -> JoinState:
    base = dict(id=1, account_id=1, chat_pk=1)
    base.update(kw)
    return JoinState(**base)


def test_join_due_rules():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

    def ago(h: float) -> str:
        return (now - timedelta(hours=h)).isoformat(timespec="seconds")

    assert join_due(None, now=now)
    assert not join_due(_state(status="member"), now=now)
    assert not join_due(_state(status="manual"), now=now)
    assert not join_due(_state(status="abandoned"), now=now)
    # бэкофф: 6ч, 12ч, 24ч, … максимум 48ч
    assert not join_due(_state(fail_count=1, last_attempt_at=ago(5)), now=now, retry_hours=6)
    assert join_due(_state(fail_count=1, last_attempt_at=ago(7)), now=now, retry_hours=6)
    assert not join_due(_state(fail_count=2, last_attempt_at=ago(11)), now=now, retry_hours=6)
    assert join_due(_state(fail_count=2, last_attempt_at=ago(13)), now=now, retry_hours=6)
    assert join_due(_state(fail_count=4, last_attempt_at=ago(49)), now=now, retry_hours=6, max_attempts=5)
    assert not join_due(_state(fail_count=5, last_attempt_at=ago(100)), now=now, max_attempts=5)
    # заявка: ждём 72ч
    assert not join_due(_state(status="requested", last_attempt_at=ago(10)), now=now)
    assert join_due(_state(status="requested", last_attempt_at=ago(80)), now=now)


def test_removal_needs_two_misses_and_prior_membership():
    assert not removal_confirmed(None)
    assert not removal_confirmed(_state(miss_count=5))  # никогда не был в чате
    assert not removal_confirmed(_state(last_member_at="2026-10-01T00:00:00+00:00", miss_count=1))
    assert removal_confirmed(_state(last_member_at="2026-10-01T00:00:00+00:00", miss_count=2))


def test_chat_search_and_paging():
    chats = [
        _chat(id=1, title="MARKET 404 | УСЛУГИ", chat_id="-1001", username="market404chat"),
        _chat(id=2, title="Услуги S&A", chat_id="-1002"),
        _chat(id=3, title="Øgma | Чат Услуг", chat_id="-1003", tag="ogma"),
        _chat(id=4, title="Услуги S&A", chat_id="-1004", username="sa_strict"),
    ]
    assert [c.id for c in filter_chats(chats, "")] == [1, 2, 3, 4]
    assert [c.id for c in filter_chats(chats, "market")] == [1]
    assert [c.id for c in filter_chats(chats, "s&a")] == [2, 4]
    assert [c.id for c in filter_chats(chats, "услуги s&a strict")] == [4]
    assert [c.id for c in filter_chats(chats, "ØGMA")] == [3]
    assert [c.id for c in filter_chats(chats, "-1003")] == [3]
    assert filter_chats(chats, "нет такого") == []

    assert page_count(0) == 1
    assert page_count(PAGE_SIZE) == 1
    assert page_count(PAGE_SIZE + 1) == 2
    assert clamp_page(99, 20) == 2
    assert clamp_page(-3, 20) == 0
    items = list(range(20))
    assert page_slice(items, 2) == [16, 17, 18, 19]
