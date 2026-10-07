from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.jobs import prefs as prefs_job
from app.jobs import setup as setup_job
from app.jobs import LogSink
from app.models import Post
from app.sender_api import SenderAPIError
from app.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "pp.db")
    await db.init()
    return db


class FakeAPI:
    calls: list[tuple] = []
    live: list[dict] = []

    def __init__(self, *a, **k) -> None:
        pass

    async def get_account(self, sid):
        return {"live": True, "spam": False}

    async def start_account(self, sid):
        FakeAPI.calls.append(("start_account", sid))

    async def put_post(self, sid, text, photo=None, **kw):
        FakeAPI.calls.append(("put_post", sid, text))
        return {}

    async def put_interval(self, sid, kind, mn, mx=None):
        FakeAPI.calls.append(("put_interval", kind))

    async def put_parallel(self, sid, value):
        FakeAPI.calls.append(("put_parallel", value))

    async def list_chats(self, sid):
        return list(FakeAPI.live)

    async def put_mentions(self, sid, enabled):
        FakeAPI.calls.append(("put_mentions", enabled))

    async def patch_chat(self, sid, cid, **fields):
        FakeAPI.calls.append(("patch_chat", cid, fields))
        return {}

    async def get_cloak(self, sid):
        return {"enabled": False, "text": ""}

    async def put_cloak(self, sid, enabled, text, **kw):
        FakeAPI.calls.append(("put_cloak", enabled))

    async def spam_start(self, sid):
        FakeAPI.calls.append(("spam_start", sid))

    async def spam_stop(self, sid):
        FakeAPI.calls.append(("spam_stop", sid))


@pytest.fixture
def api(monkeypatch):
    FakeAPI.calls = []
    FakeAPI.live = []
    monkeypatch.setattr(setup_job, "SenderAPI", FakeAPI)
    monkeypatch.setattr(prefs_job, "_api", lambda: FakeAPI())
    return FakeAPI


def patches(api, cid: str) -> list[dict]:
    return [c[2] for c in api.calls if c[0] == "patch_chat" and c[1] == cid]


async def _sender_world(store: Store, **acc_fields):
    acc = await store.add_account("tron")
    await store.update_account(acc.id, sender_account_id="sid1", **acc_fields)
    acc = await store.get_account(acc.id)
    a = await store.add_chat("Услуги A", "-1001", kind="sender")
    b = await store.add_chat("Услуги B", "-1002", kind="sender")
    posts = {
        "ru": Post(lang="ru", text="default text"),
        "en": Post(lang="en"),
        "ru_short": Post(lang="ru_short"),
        "en_short": Post(lang="en_short"),
    }
    job = await store.create_job("t", acc.id)
    return acc, a, b, posts, LogSink(store, job.id)


async def _run_sender(store, acc, a, b, posts, log):
    return await setup_job._configure_sender(store, acc, posts, [], [a, b], log)


async def test_sender_pair_off_deactivates_only_that_chat(store, api):
    acc, a, b, posts, log = await _sender_world(store)
    api.live = [{"chat_id": "-1001"}, {"chat_id": "-1002"}]
    await store.set_chat_send_enabled(acc.id, b.id, False)

    n = await _run_sender(store, acc, a, b, posts, log)

    assert n == 1
    assert {"active": False} in patches(api, "-1002")
    assert not any(f.get("text") for f in patches(api, "-1002"))  # текст в выключенный не льём
    assert any(f.get("text") == "default text" and f.get("active") for f in patches(api, "-1001"))
    assert ("spam_start", "sid1") in api.calls


async def test_sender_pair_text_override_is_pushed_verbatim(store, api):
    acc, a, b, posts, log = await _sender_world(store)
    api.live = [{"chat_id": "-1001"}, {"chat_id": "-1002"}]
    await store.set_chat_text(acc.id, b.id, "Строго\nпо структуре Øgma", [])

    await _run_sender(store, acc, a, b, posts, log)

    assert any(f.get("text") == "Строго\nпо структуре Øgma" for f in patches(api, "-1002"))
    assert any(f.get("text") == "default text" for f in patches(api, "-1001"))


async def test_sender_banned_pair_is_deactivated(store, api):
    acc, a, b, posts, log = await _sender_world(store)
    api.live = [{"chat_id": "-1001"}]
    await store.record_ban(acc.id, a.id, "send_ban", "banned")

    n = await _run_sender(store, acc, a, b, posts, log)

    assert n == 0
    assert patches(api, "-1001") == [{"active": False}] or {"active": False} in patches(api, "-1001")
    assert ("spam_start", "sid1") not in api.calls


async def test_sender_daily_limit_sets_chat_interval(store, api):
    acc, a, b, posts, log = await _sender_world(store)
    api.live = [{"chat_id": "-1001"}]
    a = await store.update_chat(a.id, max_posts_per_account=3)

    await _run_sender(store, acc, a, b, posts, log)

    assert {"interval": 28800} in patches(api, "-1001")


async def test_sender_off_stops_and_skips_everything(store, api):
    acc, a, b, posts, log = await _sender_world(store, sender_enabled=0)
    api.live = [{"chat_id": "-1001"}]

    n = await _run_sender(store, acc, a, b, posts, log)

    assert n == 0
    assert ("spam_stop", "sid1") in api.calls
    assert not any(c[0] in {"put_post", "spam_start", "patch_chat"} for c in api.calls)


# ---- schedule ---------------------------------------------------------------


@pytest.fixture
def sched(monkeypatch):
    rec = SimpleNamespace(plans=[], cleared=[])
    monkeypatch.setattr(
        setup_job, "get_settings", lambda: Settings(_env_file=None, start_hour=9)
    )

    async def fake_lookup(client, chat):
        return SimpleNamespace(title=chat.title)

    async def fake_photos(client, entity):
        return True

    async def fake_plan(**kw):
        rec.plans.append(kw)
        return {
            "title": "T", "planned": 1, "success": 1, "error": "", "first_time": "09:00",
            "cleared": 0, "mode": "post", "used_photo": False, "media_blocked": False,
        }

    async def fake_clear(client, chat, account, reason, log):
        rec.cleared.append((account.id, chat.id, reason))
        return 2

    monkeypatch.setattr(setup_job, "lookup_entity", fake_lookup)
    monkeypatch.setattr(setup_job, "peer_allows_photos", fake_photos)
    monkeypatch.setattr(setup_job, "schedule_chat_posts", fake_plan)
    monkeypatch.setattr(setup_job, "clear_scheduled_for_pair", fake_clear)
    return rec


async def _sched_world(store: Store, **chat_fields):
    acc = await store.add_account("tron")
    chat = await store.add_chat("Øgma", "-1003", kind="schedule")
    if chat_fields:
        chat = await store.update_chat(chat.id, **chat_fields)
    posts = {
        "ru": Post(lang="ru", text="default text", photo_path="/tmp/p.jpg"),
        "en": Post(lang="en"),
        "ru_short": Post(lang="ru_short"),
        "en_short": Post(lang="en_short"),
    }
    job = await store.create_job("t", acc.id)
    return acc, chat, posts, LogSink(store, job.id)


async def test_schedule_pair_off_clears_and_skips(store, sched):
    acc, chat, posts, log = await _sched_world(store)
    await store.set_chat_send_enabled(acc.id, chat.id, False)

    out = await setup_job.schedule_one_chat(store, object(), acc, chat, posts, log)

    assert out == "disabled"
    assert sched.plans == []
    assert sched.cleared == [(acc.id, chat.id, "pair_off")]


async def test_schedule_banned_pair_is_skipped(store, sched):
    acc, chat, posts, log = await _sched_world(store)
    await store.record_ban(acc.id, chat.id, "removed")

    out = await setup_job.schedule_one_chat(store, object(), acc, chat, posts, log)

    assert out == "disabled" and sched.plans == []
    assert sched.cleared[0][2] == "banned"


async def test_schedule_uses_pair_text_without_photo(store, sched):
    acc, chat, posts, log = await _sched_world(store)
    await store.set_chat_text(acc.id, chat.id, "Строго по структуре", [])

    out = await setup_job.schedule_one_chat(store, object(), acc, chat, posts, log)

    assert out == "ok"
    plan = sched.plans[0]
    assert plan["text"] == "Строго по структуре"
    assert plan["photo_path"] is None


async def test_schedule_default_text_keeps_photo(store, sched):
    acc, chat, posts, log = await _sched_world(store)

    await setup_job.schedule_one_chat(store, object(), acc, chat, posts, log)

    plan = sched.plans[0]
    assert plan["text"] == "default text" and plan["photo_path"] == "/tmp/p.jpg"


async def test_schedule_limit_stretches_interval(store, sched):
    acc, chat, posts, log = await _sched_world(store, interval_minutes=60, max_posts_per_account=3)

    await setup_job.schedule_one_chat(store, object(), acc, chat, posts, log)

    assert sched.plans[0]["interval_minutes"] == 480


async def test_schedule_send_ban_error_lands_in_ban_base(store, sched, monkeypatch):
    acc, chat, posts, log = await _sched_world(store)

    async def banned_plan(**kw):
        return {
            "title": "T", "planned": 3, "success": 0,
            "error": "UserBannedInChannelError: You're banned from sending messages",
            "first_time": "", "cleared": 0, "mode": "post", "used_photo": False,
            "media_blocked": False,
        }

    monkeypatch.setattr(setup_job, "schedule_chat_posts", banned_plan)

    out = await setup_job.schedule_one_chat(store, object(), acc, chat, posts, log)

    assert out == "skipped"
    ban = await store.get_ban(acc.id, chat.id)
    assert ban is not None and ban.active and ban.reason == "send_ban"


# ---- быстрое применение пары ------------------------------------------------


async def test_apply_pair_sender_off_and_on(store, api):
    acc, a, b, posts, log = await _sender_world(store)
    await store.save_post(acc.id, "ru", "account post", [])
    api.live = [{"chat_id": "-1001"}]

    await store.set_chat_send_enabled(acc.id, a.id, False)
    msg = await prefs_job.apply_pair(store, acc.id, a.id)
    assert "выключил" in msg and {"active": False} in patches(api, "-1001")

    api.calls.clear()
    await store.set_chat_send_enabled(acc.id, a.id, True)
    await store.set_chat_text(acc.id, a.id, "свой", [])
    msg = await prefs_job.apply_pair(store, acc.id, a.id)
    assert "включил" in msg
    assert any(f.get("text") == "свой" and f.get("active") for f in patches(api, "-1001"))
    assert ("spam_start", "sid1") in api.calls  # рассылка не шла — запускаем


async def test_apply_pair_reports_missing_live_chat(store, api):
    acc, a, b, posts, log = await _sender_world(store)
    api.live = []
    msg = await prefs_job.apply_pair(store, acc.id, a.id)
    assert "нет среди диалогов" in msg


async def test_apply_pair_without_text_does_not_push_empty(store, api):
    acc, a, b, posts, log = await _sender_world(store)
    api.live = [{"chat_id": "-1001"}]
    msg = await prefs_job.apply_pair(store, acc.id, a.id)
    assert "нет текста" in msg
    assert patches(api, "-1001") == []


async def test_apply_pair_schedule_disabled_clears_and_enabled_replans(store, monkeypatch):
    acc = await store.add_account("tron")
    await store.update_account(acc.id, telethon_session="/tmp/x.session")
    chat = await store.add_chat("Sched", "-1004", kind="schedule")
    cleared, replanned = [], []

    @asynccontextmanager
    async def fake_client(path):
        yield object()

    async def fake_clear(client, chat, account, reason, log):
        cleared.append(reason)
        return 5

    async def fake_setup(store, account_id, pks, bot, admin, **kw):
        replanned.append(list(pks))
        return {"ok": ["Sched"], "skipped": [], "abandoned": [], "config_error": []}

    monkeypatch.setattr(prefs_job, "telethon_client", fake_client)
    monkeypatch.setattr(prefs_job, "clear_scheduled_for_pair", fake_clear)
    monkeypatch.setattr(prefs_job, "run_setup_chats_only", fake_setup)

    await store.set_chat_send_enabled(acc.id, chat.id, False)
    msg = await prefs_job.apply_pair(store, acc.id, chat.id)
    assert cleared == ["pair_off"] and "5" in msg and replanned == []

    await store.set_chat_send_enabled(acc.id, chat.id, True)
    msg = await prefs_job.apply_pair(store, acc.id, chat.id)
    assert replanned == [[chat.id]] and "перепланирован" in msg


async def test_apply_account_sender_toggle(store, api, monkeypatch):
    acc = await store.add_account("tron")
    await store.update_account(acc.id, sender_account_id="sid1", sender_enabled=0)
    assert "остановлен" in await prefs_job.apply_account_sender(store, acc.id)
    assert ("spam_stop", "sid1") in api.calls

    refreshed = []

    async def fake_refresh(store, account_id, bot, admin, **kw):
        refreshed.append(account_id)
        return 4

    monkeypatch.setattr(prefs_job, "run_sender_refresh", fake_refresh)
    await store.update_account(acc.id, sender_enabled=1)
    msg = await prefs_job.apply_account_sender(store, acc.id)
    assert refreshed == [acc.id] and "4" in msg


async def test_apply_chat_limit_sender_and_clear(store, api):
    acc, a, b, posts, log = await _sender_world(store)
    api.live = [{"chat_id": "-1001"}]
    a = await store.update_chat(a.id, max_posts_per_account=3)
    msg = await prefs_job.apply_chat_limit(store, a.id)
    assert {"interval": 28800} in patches(api, "-1001") and "3 постов" in msg

    a = await store.update_chat(a.id, max_posts_per_account=0)
    await prefs_job.apply_chat_limit(store, a.id)
    assert {"clear_interval": True} in patches(api, "-1001")
