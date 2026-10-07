from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.jobs import LogSink
from app.jobs import prefs as prefs_job
from app.jobs import setup as setup_job
from app.jobs import spam as spam_job
from app.jobs import stoplist as stoplist_job
from app.models import Post
from app.store import Store
from app.utils.spamstate import SpamStatus, parse_spambot_reply

from test_pair_policy import FakeAPI, patches  # noqa: E402

LIMITED = parse_spambot_reply("К сожалению, ваш аккаунт ограничен до 9 января 2027")
CLEAN = parse_spambot_reply("Good news, no limits are currently applied to your account.")


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "spam.db")
    await db.init()
    return db


@pytest.fixture
def api(monkeypatch):
    FakeAPI.calls = []
    FakeAPI.live = []
    monkeypatch.setattr(setup_job, "SenderAPI", FakeAPI)
    monkeypatch.setattr(spam_job, "SenderAPI", FakeAPI)
    monkeypatch.setattr(prefs_job, "_api", lambda: FakeAPI())
    monkeypatch.setattr(stoplist_job, "SenderAPI", FakeAPI)
    return FakeAPI


@pytest.fixture
def spambot(monkeypatch):
    box = SimpleNamespace(status=CLEAN, calls=0)

    async def fake(client, **kw):
        box.calls += 1
        return box.status

    monkeypatch.setattr(spam_job, "check_spambot", fake)
    return box


async def _world(store: Store, **acc_fields):
    acc = await store.add_account("tron")
    await store.update_account(acc.id, sender_account_id="sid1", telethon_session="/tmp/t.s", **acc_fields)
    acc = await store.get_account(acc.id)
    a = await store.add_chat("Услуги A", "-1001", kind="sender")
    b = await store.add_chat("Услуги B", "-1002", kind="sender")
    return acc, a, b


# ---- проверка @SpamBot и dead ----------------------------------------------


async def test_check_updates_state_and_three_spamblocks_make_dead(store, spambot):
    acc, *_ = await _world(store)
    t = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    for strike in (1, 2, 3):
        spambot.status = LIMITED
        res = await spam_job.run_spam_check(store, await store.get_account(acc.id), object(), force=True, now=t)
        assert "limited_new" in res.events
        st = await store.get_spam_state(acc.id)
        assert st.is_limited and st.strikes == strike
        t += timedelta(hours=10)
        spambot.status = CLEAN
        res = await spam_job.run_spam_check(store, await store.get_account(acc.id), object(), force=True, now=t)
        assert res.events == ["cleared"] or "dead" not in res.events
        t += timedelta(days=1)
        if strike < 3:
            assert not (await store.get_account(acc.id)).is_dead
    assert (await store.get_account(acc.id)).is_dead


async def test_third_strike_flips_dead_immediately(store, spambot):
    acc, *_ = await _world(store)
    st = await store.get_spam_state(acc.id)
    st.strikes = 2
    await store.save_spam_state(st)
    spambot.status = LIMITED
    res = await spam_job.run_spam_check(store, acc, object(), force=True)
    assert "dead" in res.events
    assert (await store.get_account(acc.id)).is_dead


async def test_check_respects_schedule_and_skips_dead(store, spambot):
    acc, *_ = await _world(store)
    await spam_job.run_spam_check(store, acc, object())
    assert spambot.calls == 1
    res = await spam_job.run_spam_check(store, acc, object())  # ещё рано
    assert res.skipped == "рано" and spambot.calls == 1
    await store.update_account(acc.id, dead=1)
    res = await spam_job.run_spam_check(store, await store.get_account(acc.id), object(), force=False)
    assert res.skipped == "dead" and spambot.calls == 1


async def test_spambot_failure_does_not_change_state_or_retry_every_tick(store, monkeypatch):
    acc, *_ = await _world(store)

    async def boom(client, **kw):
        raise RuntimeError("flood")

    monkeypatch.setattr(spam_job, "check_spambot", boom)
    res = await spam_job.run_spam_check(store, acc, object())
    assert "flood" in res.error
    st = await store.get_spam_state(acc.id)
    assert st.status == "clean" and st.last_check_at
    res2 = await spam_job.run_spam_check(store, acc, object())
    assert res2.skipped == "рано"  # не долбим @SpamBot каждый проход


async def test_unrecognised_reply_is_not_a_verdict(store, spambot):
    acc, *_ = await _world(store)
    spambot.status = SpamStatus(known=False, text="???")
    res = await spam_job.run_spam_check(store, acc, object(), force=True)
    assert res.error and not res.events
    assert (await store.get_spam_state(acc.id)).last_check_at == ""


async def test_account_load_follows_ladder(store):
    acc, *_ = await _world(store)
    now = datetime.now(timezone.utc)
    assert await spam_job.account_load(store, acc) == 100
    st = await store.get_spam_state(acc.id)
    st.status, st.limited_since = "limited", (now - timedelta(hours=1)).isoformat()
    await store.save_spam_state(st)
    assert await spam_job.account_load(store, acc) == 50
    st.limited_since = (now - timedelta(hours=10)).isoformat()
    await store.save_spam_state(st)
    assert await spam_job.account_load(store, acc) == 25
    st.limited_since = (now - timedelta(hours=30)).isoformat()
    await store.save_spam_state(st)
    assert await spam_job.account_load(store, acc) == 0
    await store.update_account(acc.id, dead=1)
    assert await spam_job.account_load(store, await store.get_account(acc.id)) == 0


# ---- sender: нагрузка, dead, schedule не трогаем ---------------------------


async def _sender_setup(store, acc, a, b):
    posts = {
        "ru": Post(lang="ru", text="default text"),
        "en": Post(lang="en"),
        "ru_short": Post(lang="ru_short"),
        "en_short": Post(lang="en_short"),
    }
    job = await store.create_job("t", acc.id)
    return await setup_job._configure_sender(store, acc, posts, [], [a, b], LogSink(store, job.id))


def intervals(api):
    return {c[1]: c for c in api.calls if c[0] == "put_interval"}


async def test_spamblock_scales_sender_intervals_and_parallel(store, api):
    acc, a, b = await _world(store)
    api.live = [{"chat_id": "-1001"}, {"chat_id": "-1002"}]
    n = await _sender_setup(store, acc, a, b)
    assert n == 2
    assert [c for c in api.calls if c[0] == "put_parallel"] == [("put_parallel", 4)]

    api.calls.clear()
    st = await store.get_spam_state(acc.id)
    st.status, st.limited_since = "limited", datetime.now(timezone.utc).isoformat()
    await store.save_spam_state(st)
    n = await _sender_setup(store, acc, a, b)
    assert n == 2  # sender не остановлен, но вдвое медленнее
    assert [c for c in api.calls if c[0] == "put_parallel"] == [("put_parallel", 2)]
    assert ("spam_start", "sid1") in api.calls
    assert (await store.get_spam_state(acc.id)).applied_load == 50


async def test_long_spamblock_stops_sender_completely(store, api):
    acc, a, b = await _world(store)
    api.live = [{"chat_id": "-1001"}]
    st = await store.get_spam_state(acc.id)
    st.status = "limited"
    st.limited_since = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    await store.save_spam_state(st)

    n = await _sender_setup(store, acc, a, b)

    assert n == 0
    assert ("spam_stop", "sid1") in api.calls
    assert not any(c[0] in {"put_post", "spam_start", "patch_chat", "put_interval"} for c in api.calls)
    assert (await store.get_spam_state(acc.id)).applied_load == 0


async def test_dead_account_sender_is_stopped_forever(store, api):
    acc, a, b = await _world(store, dead=1)
    api.live = [{"chat_id": "-1001"}]
    assert await _sender_setup(store, acc, a, b) == 0
    assert ("spam_stop", "sid1") in api.calls
    assert not any(c[0] in {"put_post", "spam_start"} for c in api.calls)


async def test_recovery_runs_at_half_load_then_full(store, api):
    acc, a, b = await _world(store)
    api.live = [{"chat_id": "-1001"}]
    st = await store.get_spam_state(acc.id)
    st.cleared_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await store.save_spam_state(st)
    await _sender_setup(store, acc, a, b)
    assert (await store.get_spam_state(acc.id)).applied_load == 50
    st.cleared_at = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    await store.save_spam_state(st)
    api.calls.clear()
    await _sender_setup(store, acc, a, b)
    assert (await store.get_spam_state(acc.id)).applied_load == 100


async def test_daily_limit_interval_is_stretched_by_load(store, api):
    acc, a, b = await _world(store)
    api.live = [{"chat_id": "-1001"}]
    a = await store.update_chat(a.id, max_posts_per_account=3)
    st = await store.get_spam_state(acc.id)
    st.status, st.limited_since = "limited", datetime.now(timezone.utc).isoformat()
    await store.save_spam_state(st)
    await _sender_setup(store, acc, a, b)
    assert {"interval": 28800 * 2} in patches(api, "-1001")


async def test_schedule_is_untouched_by_spamblock_and_dead(store, monkeypatch):
    """Schedule-чаты при спамблоке и dead работают как прежде."""
    acc, *_ = await _world(store, dead=1)
    sched = await store.add_chat("Sched", "-1003", kind="schedule")
    st = await store.get_spam_state(acc.id)
    st.status, st.limited_since = "limited", datetime.now(timezone.utc).isoformat()
    await store.save_spam_state(st)
    plans = []

    async def fake_lookup(client, chat):
        return SimpleNamespace(title=chat.title)

    async def fake_photos(client, entity):
        return True

    async def fake_plan(**kw):
        plans.append(kw)
        return {"title": "T", "planned": 1, "success": 1, "error": "", "first_time": "09:00",
                "cleared": 0, "mode": "post", "used_photo": False, "media_blocked": False}

    monkeypatch.setattr(setup_job, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(setup_job, "lookup_entity", fake_lookup)
    monkeypatch.setattr(setup_job, "peer_allows_photos", fake_photos)
    monkeypatch.setattr(setup_job, "schedule_chat_posts", fake_plan)
    posts = {"ru": Post(lang="ru", text="hello"), "en": Post(lang="en"),
             "ru_short": Post(lang="ru_short"), "en_short": Post(lang="en_short")}
    job = await store.create_job("t", acc.id)

    out = await setup_job.schedule_one_chat(
        store, object(), await store.get_account(acc.id), sched, posts, LogSink(store, job.id)
    )
    assert out == "ok" and plans and plans[0]["text"] == "hello"


# ---- sync_sender_load / переключатели --------------------------------------


async def test_sync_sender_load_stops_for_dead_and_manual_off(store, api):
    acc, *_ = await _world(store, dead=1)
    msg = await spam_job.sync_sender_load(store, acc.id)
    assert "dead" in msg and ("spam_stop", "sid1") in api.calls
    assert (await store.get_spam_state(acc.id)).applied_load == 0

    api.calls.clear()
    await store.update_account(acc.id, dead=0, sender_enabled=0)
    msg = await spam_job.sync_sender_load(store, acc.id)
    assert "отключён вручную" in msg and ("spam_stop", "sid1") in api.calls


async def test_sync_sender_load_refreshes_when_allowed(store, api, monkeypatch):
    acc, *_ = await _world(store)
    called = []

    async def fake_refresh(store, account_id, bot, admin, **kw):
        called.append(account_id)
        return 3

    monkeypatch.setattr(setup_job, "run_sender_refresh", fake_refresh)
    msg = await spam_job.sync_sender_load(store, acc.id)
    assert called == [acc.id] and "100%" in msg and "3" in msg


async def test_apply_pair_respects_dead_and_spamblock(store, api):
    acc, a, b = await _world(store)
    await store.save_post(acc.id, "ru", "text", [])
    api.live = [{"chat_id": "-1001"}]
    await store.update_account(acc.id, dead=1)
    msg = await prefs_job.apply_pair(store, acc.id, a.id)
    assert "dead" in msg and {"active": False} in patches(api, "-1001")


# ---- стоп-лист «писать нельзя» ---------------------------------------------


async def test_sweep_deactivates_stoplisted_live_chats_even_outside_catalog(store, api):
    acc, a, b = await _world(store)
    flagged = await store.add_chat("Секретный", "-1004", kind="sender")
    await store.update_chat(flagged.id, no_post=1)
    api.live = [
        {"chat_id": "-1001", "title": "Услуги A", "active": True},
        {"chat_id": "-5555", "title": "Отзывы клиентов", "active": True},  # вне каталога
        {"chat_id": "-1004", "title": "Секретный", "active": True},  # флаг no_post
        {"chat_id": "-6666", "title": "Reviews & feedback", "active": True},
        {"chat_id": "-7777", "title": "Отзывы старые", "active": False},  # уже выключен
    ]
    done = await stoplist_job.sweep_stoplist(store, acc)
    assert set(done) == {"Отзывы клиентов", "Секретный", "Reviews & feedback"}
    assert {"active": False} in patches(api, "-5555")
    assert patches(api, "-1001") == [] and patches(api, "-7777") == []


async def test_sweep_does_nothing_without_rules(store, api):
    acc, *_ = await _world(store)
    await store.set_stoplist([])
    api.live = [{"chat_id": "-5555", "title": "Отзывы", "active": True}]
    assert await stoplist_job.sweep_stoplist(store, acc) == []
    assert not any(c[0] == "patch_chat" for c in api.calls)


async def test_configure_sender_never_posts_into_stoplisted_live_chat(store, api):
    acc, a, b = await _world(store)
    api.live = [
        {"chat_id": "-1001", "title": "Услуги A"},
        {"chat_id": "-5555", "title": "Отзывы о сервисах"},
    ]
    n = await _sender_setup(store, acc, a, b)
    assert n == 1
    assert {"active": False} in patches(api, "-5555")
    assert not any("text" in f for f in patches(api, "-5555"))
    assert any(f.get("text") == "default text" for f in patches(api, "-1001"))


async def test_no_post_flag_blocks_catalog_chat_for_sender_and_schedule(store, api, monkeypatch):
    acc, a, b = await _world(store)
    await store.update_chat(a.id, no_post=1)
    a = await store.get_chat(a.id)
    api.live = [{"chat_id": "-1001", "title": "Услуги A"}]
    assert await _sender_setup(store, acc, a, b) == 0
    assert {"active": False} in patches(api, "-1001")

    # schedule: «disabled», запланированное убирается, ничего не планируется
    sched = await store.add_chat("Reviews", "-1009", kind="schedule")
    cleared, planned = [], []

    async def fake_clear(client, chat, account, reason, log):
        cleared.append(reason)
        return 1

    async def fake_plan(**kw):
        planned.append(kw)

    monkeypatch.setattr(setup_job, "clear_scheduled_for_pair", fake_clear)
    monkeypatch.setattr(setup_job, "schedule_chat_posts", fake_plan)
    posts = {"ru": Post(lang="ru", text="x"), "en": Post(lang="en"),
             "ru_short": Post(lang="ru_short"), "en_short": Post(lang="en_short")}
    job = await store.create_job("t", acc.id)
    out = await setup_job.schedule_one_chat(store, object(), acc, sched, posts, LogSink(store, job.id))
    assert out == "disabled" and cleared == ["no_post"] and planned == []


async def test_stoplist_storage_normalises(store):
    assert await store.get_stoplist() == ("review", "отзыв")  # по умолчанию
    assert await store.set_stoplist([" Отзыв ", "REVIEW", "", "Feedback", "review"]) == (
        "feedback", "review", "отзыв",
    )
    assert await store.set_stoplist([]) == ()


async def test_apply_chat_all_and_stoplist_everywhere(store, api):
    acc, a, b = await _world(store)
    await store.save_post(acc.id, "ru", "text", [])
    rev = await store.add_chat("Отзывы", "-1010", kind="sender")
    api.live = [
        {"chat_id": "-1001", "title": "Услуги A", "active": True},
        {"chat_id": "-1010", "title": "Отзывы", "active": True},
        {"chat_id": "-9999", "title": "Мои отзывы", "active": True},
    ]
    msg = await stoplist_job.apply_stoplist_everywhere(store)
    assert "каталожных чатов 1" in msg
    assert {"active": False} in patches(api, "-1010")
    assert {"active": False} in patches(api, "-9999")
    assert patches(api, "-1001") == []
