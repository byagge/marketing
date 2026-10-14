"""Клоакинг аутрич-аккаунтов через маркетолога (не sender)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.store import Store
from app.tg.outreach_cloak import process_outreach_cloak


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.db")
    await s.init()
    return s


async def test_outreach_cloak_sends_to_them_first_skips_admin_and_us(store: Store):
    acc = await store.add_account("out1")
    await store.update_account(acc.id, outreach=1)
    acc = await store.get_account(acc.id)
    await store.update_sender_settings(cloak_enabled=True, cloak_text="привет, я клоак")

    now = datetime.now(timezone.utc).isoformat()
    # написал первым — ждёт клоак
    await store.upsert_dm_person(
        acc.id, 101, username="lead", name="Lead", started_by="them",
        first_at=now, last_in_at=now, last_msg_id=5, add_in=1,
    )
    # админ общего чата — не слать
    await store.upsert_dm_person(
        acc.id, 202, username="adm", name="Adm", started_by="them",
        first_at=now, last_in_at=now, last_msg_id=6, add_in=1,
    )
    # мы написали первыми — в pending не попадёт
    await store.upsert_dm_person(
        acc.id, 303, username="old", name="Old", started_by="us",
        first_at=now, last_in_at=now, last_msg_id=7, add_in=1,
    )

    sent: list[tuple[int, str]] = []

    class Client:
        async def get_entity(self, uid):
            return SimpleNamespace(id=int(uid))

        async def send_message(self, entity, text, **kwargs):
            sent.append((int(entity.id), text))
            return SimpleNamespace(id=1)

        def iter_messages(self, entity, limit=30):
            async def gen():
                return
                yield  # pragma: no cover
            return gen()

        async def get_permissions(self, ent, user_id):
            return SimpleNamespace(is_admin=False, is_creator=False)

    res = await process_outreach_cloak(
        store,
        Client(),
        acc,
        [],
        cloak_text="привет, я клоак",
        admin_ids={202},
        chat_entities=[],
    )
    assert res.sent == 1 and res.skipped_admin == 1
    assert sent == [(101, "привет, я клоак")]

    pending = await store.list_dm_cloak_pending(acc.id)
    assert pending == []

    # повторно не шлёт
    res2 = await process_outreach_cloak(
        store,
        Client(),
        acc,
        [],
        cloak_text="привет, я клоак",
        admin_ids={202},
    )
    assert res2.sent == 0


async def test_outreach_cloak_noop_for_regular_account(store: Store):
    acc = await store.add_account("reg")
    await store.upsert_dm_person(
        acc.id, 1, started_by="them", first_at="x", last_in_at="x", last_msg_id=1, add_in=1
    )
    sent = []

    class Client:
        async def get_entity(self, uid):
            return SimpleNamespace(id=uid)

        async def send_message(self, *a, **k):
            sent.append(1)

        def iter_messages(self, *a, **k):
            async def gen():
                return
                yield  # pragma: no cover
            return gen()

    res = await process_outreach_cloak(
        store, Client(), acc, [], cloak_text="x", admin_ids=set()
    )
    assert res.sent == 0 and sent == []


async def test_outreach_cloak_marks_already_sent(store: Store):
    acc = await store.add_account("out2")
    await store.update_account(acc.id, outreach=1)
    acc = await store.get_account(acc.id)
    await store.upsert_dm_person(
        acc.id, 55, started_by="them", first_at="x", last_in_at="x", last_msg_id=1, add_in=1
    )

    class Client:
        async def get_entity(self, uid):
            return SimpleNamespace(id=uid)

        async def send_message(self, *a, **k):
            raise AssertionError("must not send")

        def iter_messages(self, entity, limit=30):
            async def gen():
                yield SimpleNamespace(out=True, message="Уже ответил клоаком")
            return gen()

    res = await process_outreach_cloak(
        store,
        Client(),
        acc,
        [],
        cloak_text="Уже ответил клоаком",
        admin_ids=set(),
    )
    assert res.sent == 0 and res.skipped_already == 1
