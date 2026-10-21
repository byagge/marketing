"""Зависшие задачи и маршрут dead-аккаунтов."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.store import Store
from app.utils.send_policy import dead_post_mode


def test_dead_post_mode():
    assert dead_post_mode("⚖️ Скамный Суд | ЧАТ") == "skip"
    assert dead_post_mode("🐸 Plush | Чат услуг") == "plan"
    assert dead_post_mode("CARTEL | Услуги") == "dense"


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.db")
    await s.init()
    return s


async def test_fail_stale_jobs(store: Store):
    hung = await store.create_job("autopilot_setup", None)
    fresh = await store.create_job("collect_facts", None)
    daily = await store.create_job("setup_daily_nonpremium", None)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with store._connect() as db:
        await db.execute("UPDATE jobs SET started_at=? WHERE id=?", (old, hung.id))
        await db.execute("UPDATE jobs SET started_at=? WHERE id=?", (old, daily.id))
        await db.execute(
            "INSERT INTO job_logs(job_id, created_at, level, message) VALUES(?,?,?,?)",
            (daily.id, recent, "info", "ещё пишу слоты"),
        )
        await db.commit()
    n = await store.fail_stale_jobs(20)
    assert n == 1
    assert (await store.get_job(hung.id)).status == "error"
    assert (await store.get_job(fresh.id)).status == "running"
    assert (await store.get_job(daily.id)).status == "running"
