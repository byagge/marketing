from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobs import gate as gate_job
from app.store import Store
from app.tg import gate as tg_gate
from app.tg.gate import (
    GateInfo,
    extract_gate,
    merge_require_channels,
    normalize_gate_link,
    resolve_gate,
    scan_gate,
)

ME = SimpleNamespace(id=777, username="tron_acc")


def btn(text, url=None, data=None):
    b = SimpleNamespace(text=text)
    if url:
        b.url = url
    if data:
        b.data = data
    return b


def msg(mid, text, buttons=None, entities=None):
    markup = SimpleNamespace(rows=[SimpleNamespace(buttons=buttons)]) if buttons else None
    return SimpleNamespace(
        id=mid, message=text, entities=entities or [], reply_markup=markup, click=None
    )


def gate_msg(mid=10, text="@tron_acc, чтобы писать в чат, подпишитесь:"):
    return msg(
        mid,
        text,
        [
            btn("Канал", url="https://t.me/news_channel"),
            btn("Чат партнёров", url="https://t.me/+AbCdEf123"),
            btn("Я подписался", data=b"check"),
        ],
    )


def test_normalize_links():
    assert normalize_gate_link("https://t.me/News_Channel") == "@News_Channel"
    assert normalize_gate_link("t.me/news_channel?start=1") == "@news_channel"
    assert normalize_gate_link("https://t.me/+AbC123") == "https://t.me/+AbC123"
    assert normalize_gate_link("https://t.me/joinchat/AbC123") == "https://t.me/joinchat/AbC123"
    for bad in (
        "https://t.me/some_bot",  # бот
        "https://t.me/c/1234/5",  # ссылка на сообщение
        "https://t.me/channel/55",  # пост канала
        "https://t.me/addlist/xyz",  # папка
        "https://example.com/x",
        "",
    ):
        assert normalize_gate_link(bad) is None


def test_merge_require_channels_dedupes():
    assert merge_require_channels("", ["@a", "@b"]) == "@a, @b"
    assert merge_require_channels("@A, c", ["@a", "https://t.me/+x"]) == "@A, c, https://t.me/+x"
    assert merge_require_channels("@a", []) == "@a"
    assert merge_require_channels("@abcde", ["https://t.me/AbCdE"]) == "@abcde"


def test_extract_gate_by_username_mention():
    info = extract_gate(gate_msg(), ME.id, ME.username)
    assert info is not None and info.message_id == 10
    assert info.links == ["@news_channel", "https://t.me/+AbCdEf123"]
    assert info.confirm == (0, 2, "Я подписался")


def test_extract_gate_by_text_mention_entity():
    m = gate_msg(text="Пользователь, подпишитесь")
    m.entities = [SimpleNamespace(user_id=777)]
    assert extract_gate(m, ME.id, ME.username) is not None
    assert extract_gate(m, 5, "someone_else") is None


def test_not_a_gate():
    # чужое упоминание
    assert extract_gate(gate_msg(text="@vasya подпишитесь"), ME.id, ME.username) is None
    # упоминание есть, но без inline-кнопок
    assert extract_gate(msg(1, "@tron_acc привет, вот t.me/news_channel"), ME.id, ME.username) is None
    # кнопки без t.me-ссылок
    m = msg(1, "@tron_acc привет", [btn("Правила", url="https://example.com/rules")])
    assert extract_gate(m, ME.id, ME.username) is None
    # ссылка только на сам чат и на бота
    m = msg(1, "@tron_acc", [btn("Чат", url="https://t.me/this_chat"), btn("Бот", url="https://t.me/guard_bot")])
    assert extract_gate(m, ME.id, ME.username, chat_username="this_chat") is None
    assert extract_gate(None, ME.id, ME.username) is None


class FakeClient:
    def __init__(self, history):
        self.history = history
        self.clicked = []

    async def get_me(self):
        return ME

    async def iter_messages(self, entity, limit=40):
        for m in self.history[:limit]:
            yield m

    async def get_messages(self, entity, ids=None):
        for m in self.history:
            if m.id == ids:
                async def click(i=None, j=None, text=None):
                    self.clicked.append((i, j, text))
                m.click = click
                return m
        return None


async def test_scan_gate_returns_freshest_matching_message():
    older = gate_msg(5)
    newer = gate_msg(9)
    client = FakeClient([msg(11, "обычное сообщение"), newer, older])
    info = await scan_gate(client, object(), ME)
    assert info.message_id == 9
    assert await scan_gate(FakeClient([msg(1, "hi")]), object(), ME) is None


async def test_resolve_gate_subscribes_then_clicks_confirm(monkeypatch):
    seen = []

    async def fake_subscribe(client, raw):
        seen.append(raw)
        return ["@news_channel", "channel+AbCdEf"]

    monkeypatch.setattr(tg_gate, "subscribe_required", fake_subscribe)
    monkeypatch.setattr(tg_gate.asyncio, "sleep", _nosleep)
    client = FakeClient([gate_msg(10)])
    info = extract_gate(gate_msg(10), ME.id, ME.username)

    res = await resolve_gate(client, object(), info)

    assert seen == ["@news_channel https://t.me/+AbCdEf123"]
    assert res.subscribed == 2 and res.confirmed == "Я подписался"
    assert client.clicked == [(0, 2, None)]


async def test_resolve_gate_does_not_click_if_nothing_subscribed(monkeypatch):
    async def fake_subscribe(client, raw):
        return ["fail:ChannelPrivateError", "flood:30"]

    monkeypatch.setattr(tg_gate, "subscribe_required", fake_subscribe)
    client = FakeClient([gate_msg(10)])
    res = await resolve_gate(client, object(), extract_gate(gate_msg(10), ME.id, ME.username))
    assert res.subscribed == 0 and res.confirmed is None and client.clicked == []


async def _nosleep(*a, **k):
    return None


# ---- обход по чатам ---------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "gate.db")
    await db.init()
    return db


@pytest.fixture
def fakes(monkeypatch):
    f = SimpleNamespace(scans={}, resolved=[], ensured=[], entity_none=set(), now=0)

    async def lookup(client, chat):
        return None if chat.id in f.entity_none else SimpleNamespace(id=chat.id)

    async def scan(client, entity, me, username=""):
        return f.scans.get(entity.id)

    async def resolve(client, entity, info):
        f.resolved.append(info.message_id)
        return tg_gate.GateResult(notes=["@news_channel"], subscribed=1, confirmed="Я подписался")

    async def ensure(client, raw):
        f.ensured.append(raw)
        return ["already"]

    monkeypatch.setattr(gate_job, "lookup_entity", lookup)
    monkeypatch.setattr(gate_job, "scan_gate", scan)
    monkeypatch.setattr(gate_job, "resolve_gate", resolve)
    monkeypatch.setattr(gate_job, "subscribe_required", ensure)
    return f


def info(mid, links=("@news_channel",)):
    return GateInfo(message_id=mid, links=list(links), confirm=(0, 0, "Я подписался"))


async def _acc(store):
    acc = await store.add_account("tron")
    return acc


async def test_gate_resolved_updates_require_channels_and_state(store, fakes):
    acc = await _acc(store)
    chat = await store.add_chat("Услуги", "-1001")
    fakes.scans[chat.id] = info(10, ["@news_channel", "https://t.me/+AbCdEf123"])

    events = await gate_job.gate_step(store, FakeClient([]), acc, [chat], pause=0)

    assert [(e.kind, e.chat) for e in events] == [("resolved", "Услуги")]
    assert "нажал «Я подписался»" in events[0].detail
    assert fakes.resolved == [10]
    st = await store.get_join_state(acc.id, chat.id)
    assert st.gate_msg_id == 10 and st.gate_count == 1 and st.gate_at
    # остальные аккаунты теперь подписываются заранее
    assert (await store.get_chat(chat.id)).require_channels == "@news_channel, https://t.me/+AbCdEf123"


async def test_same_gate_message_is_not_processed_twice_and_recheck_window(store, fakes):
    acc = await _acc(store)
    chat = await store.add_chat("Услуги", "-1001")
    fakes.scans[chat.id] = info(10)
    await gate_job.gate_step(store, FakeClient([]), acc, [chat], pause=0)

    # сразу же снова — ещё рано (recheck 12ч)
    assert await gate_job.gate_step(store, FakeClient([]), acc, [chat], pause=0) == []
    assert fakes.resolved == [10]

    # через 13 часов: то же сообщение — второй раз не подписываемся
    later = datetime.now(timezone.utc) + timedelta(hours=13)
    assert await gate_job.gate_step(store, FakeClient([]), acc, [chat], pause=0, now=later) == []
    assert fakes.resolved == [10]

    # новое сообщение-ворота — подписываемся ещё раз
    fakes.scans[chat.id] = info(25)
    later2 = later + timedelta(hours=13)
    events = await gate_job.gate_step(store, FakeClient([]), acc, [chat], pause=0, now=later2)
    assert [e.kind for e in events] == ["resolved"] and fakes.resolved == [10, 25]


async def test_gate_that_keeps_coming_back_escalates_once(store, fakes):
    acc = await _acc(store)
    chat = await store.add_chat("Упрямый", "-1001")
    t = datetime.now(timezone.utc)
    kinds = []
    for mid in (10, 20, 30, 40, 50, 60):
        fakes.scans[chat.id] = info(mid)
        t += timedelta(hours=13)
        kinds += [e.kind for e in await gate_job.gate_step(store, FakeClient([]), acc, [chat], pause=0, now=t)]
    # 3 подписки, затем одно «не помогает», дальше молчим (не спамим)
    assert kinds == ["resolved", "resolved", "resolved", "unresolved"]
    assert fakes.resolved == [10, 20, 30]
    assert (await store.get_join_state(acc.id, chat.id)).gate_note.startswith("unresolved")


async def test_no_gate_found_is_quiet_and_budget_limits_chats(store, fakes):
    acc = await _acc(store)
    chats = [await store.add_chat(f"C{i}", f"-100{i}") for i in range(5)]

    events = await gate_job.gate_step(store, FakeClient([]), acc, chats, budget=2, pause=0)
    assert events == []
    scanned = [c for c in chats if (await store.get_join_state(acc.id, c.id)) is not None]
    assert len(scanned) == 2
    # следующий проход берёт тех, кого ещё не сканировали
    await gate_job.gate_step(store, FakeClient([]), acc, chats, budget=2, pause=0)
    scanned2 = [c for c in chats if (await store.get_join_state(acc.id, c.id)) is not None]
    assert len(scanned2) == 4


async def test_first_scan_subscribes_to_known_required_channels(store, fakes):
    acc = await _acc(store)
    chat = await store.add_chat("MARKET 404", "-1001")
    await store.update_chat(chat.id, require_channels="@market404chat")
    chat = await store.get_chat(chat.id)

    await gate_job.gate_step(store, FakeClient([]), acc, [chat], pause=0)
    assert fakes.ensured == ["@market404chat"]
    fakes.ensured.clear()
    later = datetime.now(timezone.utc) + timedelta(hours=13)
    await gate_job.gate_step(store, FakeClient([]), acc, [chat], pause=0, now=later)
    assert fakes.ensured == []  # только при первом заходе


async def test_one_broken_chat_does_not_stop_others(store, fakes, monkeypatch):
    acc = await _acc(store)
    c1 = await store.add_chat("Broken", "-1001")
    c2 = await store.add_chat("Fine", "-1002")
    fakes.scans[c2.id] = info(7)

    real_scan = gate_job.scan_gate

    async def flaky(client, entity, me, username=""):
        if entity.id == c1.id:
            raise RuntimeError("boom")
        return await real_scan(client, entity, me, username)

    monkeypatch.setattr(gate_job, "scan_gate", flaky)
    events = await gate_job.gate_step(store, FakeClient([]), acc, [c1, c2], pause=0)
    assert [e.chat for e in events] == ["Fine"]
    assert (await store.get_join_state(acc.id, c1.id)).gate_note.startswith("ошибка")


async def test_missing_entity_is_recorded(store, fakes):
    acc = await _acc(store)
    chat = await store.add_chat("Gone", "-1001")
    fakes.entity_none.add(chat.id)
    assert await gate_job.gate_step(store, FakeClient([]), acc, [chat], pause=0) == []
    assert (await store.get_join_state(acc.id, chat.id)).gate_note == "чат не найден"
