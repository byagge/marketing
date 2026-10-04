import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.jobs.export import build_report, build_zip, render_summary
from app.store import Store
from app.utils.balance import PairFact
from app.utils.minutes import even_spaced_minutes

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
async def store(tmp_path: Path):
    db = Store(tmp_path / "exp.db")
    await db.init()
    return db


async def _seed(store: Store):
    chat = await store.add_chat(
        "Lustify", "-1001", kind="schedule", invite_link="https://t.me/+AbCdEfGhIjKlMnOpQr"
    )
    accs = []
    for i, m in enumerate(even_spaced_minutes(60, 12)):
        a = await store.add_account(f"acc{i}")
        await store.update_account(
            a.id,
            telethon_session=f"/srv/data/sessions/secret{i}.session",
            sender_bot_token="123456:SECRET-TOKEN",
            phone="+996555123456",
        )
        await store.set_slot(chat.id, a.id, m)
        await store.record_setup_ok(a.id, chat.id, "sig")
        member = 1 if i < 6 else 0
        await store.upsert_fact(
            PairFact(a.id, chat.id, member=member, can_send=member, sent_24h=5 if member else None,
                     checked_at=NOW, scheduled_count=24 if member else None)
        )
        accs.append(a)
    await store.save_post(accs[0].id, "ru", "Привет, мир", [])
    job = await store.create_job("setup", accs[0].id)
    await store.add_log(job.id, "тестовая строка лога")
    await store.finish_job(job.id, "done", "ok")
    return chat, accs


async def test_report_has_everything_and_finds_the_hole(store):
    await _seed(store)
    data = await build_report(store)
    for key in ("meta", "accounts", "chats", "slots", "setup_states", "facts", "matrix",
                "chat_summary", "account_summary", "anomalies", "jobs", "job_logs", "posts",
                "config", "not_available", "plan_preview"):
        assert key in data, key
    codes = {a["code"] for a in data["anomalies"]}
    assert "gap_hole" in codes and "dead_slots" in codes
    [cs] = data["chat_summary"]
    assert cs["working"] == 6 and cs["not_member"] == 6
    assert cs["gap_real_min"] > cs["gap_after_plan_min"] == 10
    assert any("тестовая строка лога" == r["message"] for r in data["job_logs"])
    assert len(data["matrix"]) == 12


async def test_zip_contains_files_and_no_secrets(store):
    await _seed(store)
    data = await build_report(store)
    blob = build_zip(data)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = set(zf.namelist())
    assert {"SUMMARY.md", "report.json", "matrix.csv", "facts.csv", "job_logs.csv"} <= names
    json.loads(zf.read("report.json"))
    everything = b"".join(zf.read(n) for n in names).decode("utf-8", "ignore")
    for secret in ("SECRET-TOKEN", "secret0.session", "/srv/data/sessions", "555123456",
                   "AbCdEfGhIjKlMnOpQr"):
        assert secret not in everything, secret
    assert "Что бросается в глаза" in zf.read("SUMMARY.md").decode()


async def test_summary_mentions_what_is_missing(store):
    await _seed(store)
    text = render_summary(await build_report(store))
    assert "Чего нет в этой выгрузке" in text and "ЛС" in text


async def test_empty_db_still_exports(store):
    data = await build_report(store)
    assert data["anomalies"][0]["code"] == "no_facts"
    assert build_zip(data)
