"""Внешний сторож: если цикл маркетинга завис — перезапуск systemd.

Не трогает другие сервисы. Повторный рестарт не чаще раза в 40 минут
и не в первые 45 минут после старта (суточная настройка ещё идёт).
"""

from __future__ import annotations

import sqlite3
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path("/opt/marketing")
DB = ROOT / "data" / "marketing.db"
ENV = ROOT / ".env"
STAMP = ROOT / "data" / "stall_restart_at"
TZ = ZoneInfo("Asia/Bishkek")
GRACE_MIN = 45
COOLDOWN_MIN = 40


def _env(key: str) -> str:
    if not ENV.exists():
        return ""
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _active_enter() -> datetime | None:
    try:
        raw = subprocess.check_output(
            ["systemctl", "show", "marketing", "-p", "ActiveEnterTimestamp", "--value"],
            text=True,
        ).strip()
    except Exception:
        return None
    if not raw:
        return None
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S %z"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _reasons(now: datetime) -> list[str]:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    reasons: list[str] = []
    cutoff = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
    hard = db.execute(
        "SELECT kind, COUNT(*) n FROM jobs WHERE status='running' AND started_at < ? "
        "AND kind NOT IN ('setup_daily_nonpremium','setup','setup_retry','health') "
        "GROUP BY kind",
        (cutoff,),
    ).fetchall()
    for row in hard:
        reasons.append(f"{row['kind']} running ×{row['n']}")

    local = now.astimezone(TZ)
    if 9 <= local.hour <= 23:
        last = db.execute("SELECT MAX(sent_at) FROM send_events").fetchone()[0] or ""
        sent = _parse(last)
        if sent is None or (now - sent).total_seconds() > 30 * 60:
            # суточная настройка ещё пишет логи — отправок может не быть
            fresh = db.execute(
                "SELECT 1 FROM jobs j JOIN job_logs l ON l.job_id=j.id "
                "WHERE j.kind='setup_daily_nonpremium' AND j.status='running' "
                "AND l.created_at >= ? LIMIT 1",
                (cutoff,),
            ).fetchone()
            if not fresh:
                reasons.append("нет отправок 30 мин")

    checked = db.execute("SELECT MAX(checked_at) FROM chat_facts").fetchone()[0] or ""
    fact = _parse(checked)
    if fact is None or (now - fact).total_seconds() > 2 * 3600:
        reasons.append("факты старше 2 ч")

    done = db.execute(
        "SELECT MAX(finished_at) FROM jobs WHERE kind='setup_daily_nonpremium' AND status='done'"
    ).fetchone()[0] or ""
    daily = _parse(done)
    if (daily is None or (now - daily).total_seconds() > 26 * 3600) and local.hour >= 8:
        running = db.execute(
            "SELECT 1 FROM jobs j JOIN job_logs l ON l.job_id=j.id "
            "WHERE j.kind='setup_daily_nonpremium' AND j.status='running' "
            "AND l.created_at >= ? LIMIT 1",
            (cutoff,),
        ).fetchone()
        if not running:
            reasons.append("суточная настройка старше 26 ч")
    db.close()
    return reasons


def _alert(text: str) -> None:
    token = _env("BOT_TOKEN")
    admins = _env("ADMIN_IDS")
    if not token or not admins:
        return
    admin = admins.split(",")[0].strip()
    if not admin:
        return
    data = urllib.parse.urlencode({"chat_id": admin, "text": text[:3500]}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        urllib.request.urlopen(url, data=data, timeout=15)
    except Exception:
        pass


def main() -> None:
    now = datetime.now(timezone.utc)
    started = _active_enter()
    if started and (now - started).total_seconds() < GRACE_MIN * 60:
        print("grace")
        return
    if STAMP.exists():
        prev = _parse(STAMP.read_text(encoding="utf-8").strip())
        if prev and (now - prev).total_seconds() < COOLDOWN_MIN * 60:
            print("cooldown")
            return
    reasons = _reasons(now)
    if not reasons:
        print("ok")
        return
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
    text = "Маркетинг: авто-рестарт.\n" + "\n".join(f"· {r}" for r in reasons)
    _alert(text)
    print("restart:", "; ".join(reasons))
    subprocess.check_call(["systemctl", "restart", "marketing"])


if __name__ == "__main__":
    main()
