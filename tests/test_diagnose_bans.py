from pathlib import Path

import pytest

from app.jobs import bans as bans_job
from app.jobs.diagnose import diagnose_chat, diagnosis_html
from app.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "dg.db")
    await db.init()
    return db


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def _acc(store, label, **fields):
    acc = await store.add_account(label)
    await store.update_account(acc.id, **fields)
    await store.save_post(acc.id, "ru", "text", [])
    return await store.get_account(acc.id)


def reasons(diag) -> dict[str, list[str]]:
    return {k: v for k, v in diag.reasons.items()}


async def test_schedule_chat_with_empty_plan_is_explained(store):
    """«LOLZ: в плане 0 акк.» — диагностика должна прямо сказать, почему некому слать."""
    for label in ("trk", "a", "b"):
        await _acc(store, label, telethon_session=f"/tmp/{label}.s")
    chat = await store.add_chat("LOLZ | УСЛУГИ", "-1001", kind="schedule")

    diag = await diagnose_chat(store, chat.id)

    assert any("в плане 0" in n for n in diag.notes)
    assert reasons(diag)["нет минуты в таблице (не в плане)"] == ["trk", "a", "b"]
    assert diag.ok == []
    assert "в плане 0" in diagnosis_html(diag)


async def test_only_accounts_in_table_can_send(store):
    trk = await _acc(store, "trk", telethon_session="/tmp/t.s")
    other = await _acc(store, "other", telethon_session="/tmp/o.s")
    chat = await store.add_chat("CARTEL", "-1002", kind="schedule")
    await store.set_slot(chat.id, trk.id, 5)
    await store.record_setup_ok(trk.id, chat.id)

    diag = await diagnose_chat(store, chat.id)

    assert diag.ok == ["trk"]
    assert reasons(diag)["нет минуты в таблице (не в плане)"] == ["other"]


async def test_each_blocker_has_its_own_reason(store):
    off = await _acc(store, "off", telethon_session="/tmp/1.s", sender_account_id="s1")
    banned = await _acc(store, "banned", telethon_session="/tmp/2.s", sender_account_id="s2")
    nosender = await _acc(store, "nosender", telethon_session="/tmp/3.s")
    sender_off = await _acc(store, "sender_off", telethon_session="/tmp/4.s", sender_account_id="s4", sender_enabled=0)
    notext = await store.add_account("notext")
    await store.update_account(notext.id, sender_account_id="s5")
    manual = await _acc(store, "manual", telethon_session="/tmp/6.s", sender_account_id="s6")
    fine = await _acc(store, "fine", telethon_session="/tmp/7.s", sender_account_id="s7")
    chat = await store.add_chat("Услуги S&A", "-1003", kind="sender")

    await store.set_chat_send_enabled(off.id, chat.id, False)
    await store.record_ban(banned.id, chat.id, "removed")
    await store.record_join_result(manual.id, chat.id, status="manual", error="нужна заявка")

    diag = await diagnose_chat(store, chat.id)
    r = reasons(diag)

    assert r["отключено вручную для этого аккаунта"] == ["off"]
    assert r["бан в чате (removed)"] == ["banned"]
    assert r["нет sender (Sender ID / Pyrogram + token)"] == ["nosender"]
    assert r["sender у аккаунта выключен"] == ["sender_off"]
    assert r["нет текста для этого чата (ни поста аккаунта, ни своего)"] == ["notext"]
    assert r["не вступил: нужна заявка"] == ["manual"]
    assert diag.ok == ["fine"]


async def test_pair_text_counts_as_text(store):
    acc = await store.add_account("a")
    await store.update_account(acc.id, sender_account_id="s1")
    chat = await store.add_chat("Øgma", "-1004", kind="sender")
    assert "нет текста для этого чата (ни поста аккаунта, ни своего)" in (await diagnose_chat(store, chat.id)).reasons
    await store.set_chat_text(acc.id, chat.id, "Строго по структуре")
    assert (await diagnose_chat(store, chat.id)).ok == ["a"]


async def test_live_membership_overrides_ok(store):
    a = await _acc(store, "a", telethon_session="/tmp/a.s", sender_account_id="s1")
    b = await _acc(store, "b", telethon_session="/tmp/b.s", sender_account_id="s2")
    c = await _acc(store, "c", telethon_session="/tmp/c.s", sender_account_id="s3")
    chat = await store.add_chat("MARKET 404", "-1005", kind="sender")

    diag = await diagnose_chat(store, chat.id, live_members={a.id: True, b.id: False, c.id: None})

    assert diag.ok == ["a"]
    assert reasons(diag)["не состоит в чате (проверка вживую)"] == ["b"]
    assert reasons(diag)["не удалось проверить членство (ошибка Telethon)"] == ["c"]


async def test_limit_and_missing_chat(store):
    chat = await store.add_chat("S&A strict", "-1006", kind="sender")
    await store.update_chat(chat.id, max_posts_per_account=3)
    diag = await diagnose_chat(store, chat.id)
    assert any("не более 3 постов" in n for n in diag.notes)
    assert await diagnose_chat(store, 9999) is None


def test_ban_advice_thresholds():
    assert "точечный" in bans_job.ban_advice(1, 30)
    assert "несколько" in bans_job.ban_advice(2, 30)
    assert "массовые" in bans_job.ban_advice(12, 30)
    assert "массовые" not in bans_job.ban_advice(3, 30)  # 10% — ещё не массовый
    assert "нет аккаунтов" in bans_job.ban_advice(1, 0)


async def test_register_ban_notifies_once_and_stores(store, monkeypatch):
    acc = await _acc(store, "tron", telethon_session="/tmp/a.s")
    chat = await store.add_chat("MARKET 404", "-1001")
    bot = FakeBot()

    ban, is_new = await bans_job.register_ban(store, acc, chat, "send_ban", "banned", bot, 42)
    assert is_new and len(bot.sent) == 1 and bot.sent[0][0] == 42
    assert "MARKET 404" in bot.sent[0][1] and "tron" in bot.sent[0][1]
    assert (await store.get_ban(acc.id, chat.id)).notified == 1

    _, again = await bans_job.register_ban(store, acc, chat, "send_ban", "banned", bot, 42)
    assert not again and len(bot.sent) == 1  # повтор не спамит


async def test_ban_report_groups_by_chat(store):
    a1 = await _acc(store, "a1", telethon_session="/tmp/1.s")
    a2 = await _acc(store, "a2", telethon_session="/tmp/2.s")
    a3 = await _acc(store, "a3", telethon_session="/tmp/3.s")
    c1 = await store.add_chat("Strict <chat>", "-1001")
    c2 = await store.add_chat("Calm", "-1002")
    await store.record_ban(a1.id, c1.id, "join_ban")
    await store.record_ban(a2.id, c1.id, "removed")
    await store.record_ban(a3.id, c2.id, "send_ban")

    html = await bans_job.ban_report_html(store)

    assert "активных: 3 в 2 чатах" in html
    assert "Strict &lt;chat&gt;" in html  # экранирование
    assert html.index("Strict") < html.index("Calm")  # больше банов — выше
    assert "a1" in html and "a2" in html and "a3" in html

    await store.clear_ban(a3.id, c2.id)
    assert "Calm" not in await bans_job.ban_report_html(store)
    await store.clear_ban(a1.id, c1.id)
    await store.clear_ban(a2.id, c1.id)
    assert "Активных банов нет" in await bans_job.ban_report_html(store)
