import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.jobs import perf as perf_job
from app.models import AccountPerf
from app.store import Store
from app.tg.dmstats import DmStats, collect_dm_stats
from app.utils.perf import (
    apply_measurement,
    check_due,
    evaluate_verdict,
    in_grace,
    mark_redesigned,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def measure(prev, wrote, *, new=0, cloak=-1, now=NOW, **kw):
    return apply_measurement(
        prev, new_n=new, wrote_n=wrote, cloak_n=cloak, scanned=wrote, truncated=False,
        window_days=7, now=now, low_threshold=3, grace_days=7, renotify_days=7, **kw,
    )


def test_verdict_thresholds():
    assert evaluate_verdict(0) == "none"
    assert evaluate_verdict(1) == "low" and evaluate_verdict(2) == "low"
    assert evaluate_verdict(3) == "ok" and evaluate_verdict(50) == "ok"
    assert evaluate_verdict(4, low_threshold=5) == "low"


def test_flag_lifecycle():
    prev = AccountPerf(account_id=1)
    out = measure(prev, 0)
    assert out.event == "flagged" and out.perf.verdict == "none" and out.perf.flagged
    assert out.perf.flagged_at == out.perf.notified_at

    # на следующей проверке — тишина (уже сообщили)
    out2 = measure(out.perf, 1, now=NOW + timedelta(hours=12))
    assert out2.event == "" and out2.perf.verdict == "low" and out2.perf.flagged
    assert out2.perf.flagged_at == out.perf.flagged_at  # «с какого числа в списке» не прыгает

    # через неделю всё ещё плохо — напоминание
    out3 = measure(out2.perf, 0, now=NOW + timedelta(days=7, hours=1))
    assert out3.event == "reminder" and out3.perf.notified_at != out.perf.notified_at

    # аккаунт ожил
    out4 = measure(out3.perf, 9, now=NOW + timedelta(days=8))
    assert out4.event == "recovered" and not out4.perf.flagged and out4.perf.verdict == "ok"
    assert measure(out4.perf, 9, now=NOW + timedelta(days=9)).event == ""


def test_good_account_never_flagged():
    out = measure(AccountPerf(account_id=1), 12, new=5, cloak=20)
    assert out.event == "" and out.perf.verdict == "ok" and not out.perf.flagged
    assert (out.perf.new_n, out.perf.wrote_n, out.perf.cloak_n) == (5, 12, 20)


def test_redesigned_grace_period():
    flagged = measure(AccountPerf(account_id=1), 0).perf
    done = mark_redesigned(flagged, NOW)
    assert not done.flagged and done.verdict == "grace" and done.redesigned_at
    # пока идёт «льготная неделя» — не флагуем, даже если пусто
    assert in_grace(done, NOW + timedelta(days=3), 7)
    out = measure(done, 0, now=NOW + timedelta(days=3))
    assert out.event == "" and out.perf.verdict == "grace" and not out.perf.flagged
    # неделя прошла, по-прежнему никто не пишет — снова в списке
    out2 = measure(out.perf, 0, now=NOW + timedelta(days=8))
    assert out2.event == "flagged" and out2.perf.verdict == "none"
    # а если после переоформления стали писать — всё хорошо
    out3 = measure(done, 7, now=NOW + timedelta(days=8))
    assert out3.event == "" and out3.perf.verdict == "ok"


def test_check_due():
    assert check_due(AccountPerf(account_id=1), NOW, 12)
    p = AccountPerf(account_id=1, checked_at=(NOW - timedelta(hours=5)).isoformat())
    assert not check_due(p, NOW, 12) and check_due(p, NOW, 4)


# ---- сбор статистики личных сообщений ---------------------------------------


def msg(mid, when, out=False, text=""):
    return SimpleNamespace(id=mid, date=when, out=out, message=text)


class FakeClient:
    def __init__(self, dialogs, history):
        self.dialogs = dialogs  # от свежих к старым
        self.history = history  # entity_id -> [msg] от новых к старым
        self.iterated = []

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d

    async def iter_messages(self, ent, limit=None, reverse=False):
        msgs = list(self.history.get(ent.id, []))
        if reverse:
            msgs = list(reversed(msgs))
        self.iterated.append(ent.id)
        for m in msgs[:limit]:
            yield m


def dlg(uid, when, *, user=True, bot=False, self_=False, deleted=False):
    return SimpleNamespace(
        date=when,
        is_user=user,
        entity=SimpleNamespace(id=uid, bot=bot, deleted=deleted, is_self=self_),
    )


def scenario():
    since = NOW - timedelta(days=7)
    d = lambda days: NOW - timedelta(days=days)  # noqa: E731
    dialogs = [
        dlg(1, d(0.1)),  # написал первым, 3 сообщения
        dlg(2, d(1)),  # мы начали (аутрич), он ответил
        dlg(3, d(2)),  # только наши исходящие + клоакинг, он молчит
        dlg(4, d(3), bot=True),  # SpamBot и т. п.
        dlg(5, d(3), self_=True),  # Избранное
        dlg(6, d(3), user=False),  # группа
        dlg(777000, d(3)),  # служебный Telegram
        dlg(7, d(4), deleted=True),
        dlg(8, d(6)),  # написал первым 6 дней назад
        dlg(9, d(20)),  # старый — обход должен остановиться здесь
        dlg(10, d(25)),
    ]
    cloak = "Здравствуйте! Я автоответчик."
    history = {
        1: [msg(13, d(0.1)), msg(12, d(0.2), out=True, text=cloak), msg(11, d(0.3))],
        2: [msg(23, d(1)), msg(22, d(1.5), out=True, text="привет"), msg(21, d(2), out=True, text="здравствуйте")],
        3: [msg(32, d(2), out=True, text=cloak), msg(31, d(2.1), out=True, text="  Здравствуйте!   Я автоответчик. ")],
        8: [msg(81, d(6), out=True, text=cloak), msg(80, d(6.5))],
        9: [msg(90, d(20))],
        10: [msg(100, d(25))],
        7: [msg(70, d(4))],
    }
    return since, dialogs, history, cloak


async def test_collect_counts_people_new_and_cloak():
    since, dialogs, history, cloak = scenario()
    client = FakeClient(dialogs, history)
    st = await collect_dm_stats(client, since, cloak_text=cloak, pause=0)
    # писали: 1, 2, 8 (3 — только наши сообщения; боты/группа/self/служебные/удалённые не в счёт)
    assert st.wrote_n == 3
    # первыми написали: 1 и 8; у 2 диалог начали мы
    assert st.new_n == 2
    # клоакинг: диалоги 1 (1), 3 (2 — пробелы/регистр не важны), 8 (1)
    assert st.cloak_n == 4
    assert st.scanned == 4 and not st.truncated
    assert 9 not in client.iterated and 10 not in client.iterated  # старые не читаем


async def test_collect_without_cloak_text_reports_unknown():
    since, dialogs, history, _ = scenario()
    st = await collect_dm_stats(FakeClient(dialogs, history), since, cloak_text="", pause=0)
    assert st.cloak_n == -1 and st.wrote_n == 3


async def test_collect_respects_dialog_cap():
    since, dialogs, history, cloak = scenario()
    st = await collect_dm_stats(FakeClient(dialogs, history), since, max_dialogs=2, pause=0)
    assert st.truncated and st.scanned == 2


async def test_collect_empty_account():
    st = await collect_dm_stats(FakeClient([], {}), NOW - timedelta(days=7), cloak_text="x", pause=0)
    assert (st.wrote_n, st.new_n, st.cloak_n, st.scanned) == (0, 0, 0, 0)


# ---- perf_step --------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "perf.db")
    await db.init()
    return db


@pytest.fixture
def stats(monkeypatch):
    box = SimpleNamespace(value=DmStats(0, 0, -1, 5, False), calls=[], raises=False)

    async def fake(client, since, **kw):
        box.calls.append((since, kw))
        if box.raises:
            raise RuntimeError("flood")
        return box.value

    monkeypatch.setattr(perf_job, "collect_dm_stats", fake)
    monkeypatch.setattr(perf_job, "get_settings", lambda: Settings(_env_file=None))
    return box


async def _old_account(store, label="tron", days=30):
    acc = await store.add_account(label)
    created = (NOW - timedelta(days=days)).isoformat()
    async with store._connect() as db:
        await db.execute("UPDATE accounts SET created_at=? WHERE id=?", (created, acc.id))
        await db.commit()
    return await store.get_account(acc.id)


async def test_new_account_is_not_judged_yet(store, stats):
    acc = await _old_account(store, days=2)
    assert await perf_job.perf_step(store, object(), acc, now=NOW) is None
    assert stats.calls == [] and not (await store.get_perf(acc.id)).measured
    # принудительно (кнопка «проверить сейчас») — меряем
    await perf_job.perf_step(store, object(), acc, now=NOW, force=True)
    assert len(stats.calls) == 1


async def test_flagged_then_quiet_then_recovered(store, stats):
    acc = await _old_account(store)
    ev = await perf_job.perf_step(store, object(), acc, now=NOW)
    assert ev.kind == "flagged" and ev.perf.verdict == "none"
    assert "написали 0 чел." in perf_job.describe_stats(ev.perf)

    # слишком рано для новой проверки
    assert await perf_job.perf_step(store, object(), acc, now=NOW + timedelta(hours=3)) is None
    assert len(stats.calls) == 1
    # 13 часов спустя — ещё плохо, но уже сообщали: тишина
    assert await perf_job.perf_step(store, object(), acc, now=NOW + timedelta(hours=13)) is None
    # аккаунт начали писать
    stats.value = DmStats(new_n=3, wrote_n=8, cloak_n=11, scanned=10, truncated=False)
    ev = await perf_job.perf_step(store, object(), acc, now=NOW + timedelta(days=2))
    assert ev.kind == "recovered"
    saved = await store.get_perf(acc.id)
    assert not saved.flagged and saved.wrote_n == 8 and saved.cloak_n == 11


async def test_skip_flag_and_errors(store, stats):
    acc = await _old_account(store)
    await perf_job.toggle_account_skip(store, acc.id)
    assert await perf_job.perf_step(store, object(), acc, now=NOW) is None and stats.calls == []
    await perf_job.toggle_account_skip(store, acc.id)

    stats.raises = True  # FloodWait и т. п.: не падаем и не долбим каждый проход
    assert await perf_job.perf_step(store, object(), acc, now=NOW) is None
    assert (await store.get_perf(acc.id)).measured
    stats.raises = False
    assert await perf_job.perf_step(store, object(), acc, now=NOW + timedelta(hours=1)) is None
    assert len(stats.calls) == 1  # окно между проверками соблюдено (была только упавшая попытка)


async def test_cloak_text_only_counted_when_cloak_enabled(store, stats):
    acc = await _old_account(store)
    await store.update_sender_settings(cloak_enabled=False, cloak_text="Привет")
    await perf_job.perf_step(store, object(), acc, now=NOW, force=True)
    assert stats.calls[-1][1]["cloak_text"] == ""
    await store.update_sender_settings(cloak_enabled=True, cloak_text="Привет")
    await perf_job.perf_step(store, object(), acc, now=NOW, force=True)
    assert stats.calls[-1][1]["cloak_text"] == "Привет"
    since = stats.calls[-1][0]
    assert since == NOW - timedelta(days=7)


# ---- отчёт и кнопки ---------------------------------------------------------


async def test_report_lists_flagged_and_actions(store, stats):
    a = await _old_account(store, "tron")
    b = await _old_account(store, "alex")
    c = await _old_account(store, "good")
    await store.update_account(a.id, username="tron_x")
    stats.value = DmStats(0, 0, 4, 3, False)
    await perf_job.perf_step(store, object(), await store.get_account(a.id), now=NOW)
    stats.value = DmStats(1, 2, -1, 3, False)
    await perf_job.perf_step(store, object(), b, now=NOW + timedelta(days=1))
    stats.value = DmStats(5, 20, 9, 3, False)
    await perf_job.perf_step(store, object(), c, now=NOW)

    html = await perf_job.redesign_report_html(store)
    assert "Аккаунты для переоформления</b> — 2" in html
    assert "tron (@tron_x)" in html and "никто не пишет" in html and "клоакинг сработал: 4" in html
    assert "alex" in html and "пишут мало" in html
    assert "good" not in html
    assert html.index("tron") < html.index("alex")  # дольше ждёт — выше

    await perf_job.mark_account_redesigned(store, a.id, now=NOW + timedelta(days=2))
    html = await perf_job.redesign_report_html(store)
    assert "— 1" in html and "Переоформлены, ждём результат" in html

    await perf_job.toggle_account_skip(store, b.id)
    html = await perf_job.redesign_report_html(store)
    assert "— 0" in html and "Не оцениваем: alex" in html
    assert "все измеренные аккаунты приносят клиентов" in html


async def test_report_caps_long_lists(store, stats):
    for i in range(14):
        acc = await _old_account(store, f"acc{i}")
        await perf_job.perf_step(store, object(), acc, now=NOW + timedelta(minutes=i))
    html = await perf_job.redesign_report_html(store)
    assert "— 14" in html and "и ещё 4" in html and len(html) < 3500


async def test_handlers_done_skip_now(store, stats, monkeypatch):
    from app.bot.handlers import redesign as rh
    from app.bot.keyboards import MenuCB
    from app.context import ctx

    ctx.store = store
    acc = await _old_account(store, "tron")
    await perf_job.perf_step(store, object(), acc, now=NOW)
    shown, answers = [], []

    async def fake_edit(event, text, markup=None, **kw):
        shown.append((text, markup))

    monkeypatch.setattr(rh, "safe_edit", fake_edit)

    class Q:
        from_user = SimpleNamespace(id=9)
        bot = SimpleNamespace(send_message=lambda *a, **k: asyncio.sleep(0))

        async def answer(self, text="", show_alert=False, **kw):
            answers.append(text)

    q = Q()
    await rh.cb_redesign(q, SimpleNamespace(clear=lambda: asyncio.sleep(0)))
    text, markup = shown[-1]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "tron" in text and "Переоформил: tron" in labels and "Проверить сейчас" in labels

    await rh.cb_redesign_skip(q, MenuCB(a="rd_skip", i=acc.id))
    assert (await store.get_perf(acc.id)).skip == 1 and "tron" not in shown[-1][0].split("Не оцениваем")[0]
    await rh.cb_redesign_skip(q, MenuCB(a="rd_skip", i=acc.id))
    assert (await store.get_perf(acc.id)).skip == 0

    await perf_job.perf_step(store, object(), acc, now=NOW + timedelta(days=1))
    await rh.cb_redesign_done(q, MenuCB(a="rd_done", i=acc.id))
    p = await store.get_perf(acc.id)
    assert not p.flagged and p.verdict == "grace" and p.redesigned_at
    assert "Переоформлены, ждём результат" in shown[-1][0]


async def test_scan_all_accounts(store, stats, monkeypatch):
    from contextlib import asynccontextmanager

    a = await _old_account(store, "tron")
    await store.update_account(a.id, telethon_session="/x")
    b = await store.add_account("nosession")

    @asynccontextmanager
    async def fake_client(path):
        yield object()

    monkeypatch.setattr(perf_job, "telethon_client", fake_client)
    msg_ = await perf_job.run_perf_scan(store)
    assert "Проверено аккаунтов: 1" in msg_ and "Для переоформления: 1" in msg_
    assert (await store.get_perf(b.id)).measured is False


async def test_perf_is_removed_with_account(store, stats):
    acc = await _old_account(store)
    await perf_job.perf_step(store, object(), acc, now=NOW)
    assert await store.list_perf()
    await store.delete_account(acc.id)
    assert await store.list_perf() == []


def test_account_card_perf_line():
    from app.ui.autopilot_screens import perf_line_html

    assert perf_line_html(AccountPerf(account_id=1)) == ""
    p = AccountPerf(account_id=1, checked_at="x", wrote_n=2, new_n=1, cloak_n=7, window_days=7, flagged_at="x")
    html = perf_line_html(p)
    assert "написали <b>2</b>" in html and "клоакинг сработал <b>7</b>" in html and "для переоформления" in html
    assert "<b>—</b>" in perf_line_html(AccountPerf(account_id=1, checked_at="x", cloak_n=-1))
