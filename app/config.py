from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
POSTS_DIR = DATA_DIR / "posts"
DB_PATH = DATA_DIR / "marketing.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = ""
    admin_ids: str = ""

    api_id: int = 31999582
    api_hash: str = "d1126aadf79c595b641181fd4d5df2ea"

    sender_api_url: str = "http://127.0.0.1:8787"
    sender_api_key: str = ""

    timezone: str = "Asia/Bishkek"
    start_hour: int = 9
    # Daily Telegram repeat for Premium accounts only (seconds). 0 = never send.
    repeat_period: int = 86400

    weekly_health_dow: str = "mon"
    weekly_health_hour: int = 10

    # Параллельная настройка аккаунтов (Telethon разные сессии — ок).
    # 8–10 обычно безопасно; FloodWait обрабатывается внутри.
    setup_parallel: int = 8
    setup_batch_pause_sec: float = 1.5
    # Пауза между scheduled-сообщениями внутри одного чата.
    schedule_pause_sec: float = 0.22
    schedule_delete_pause_sec: float = 0.12

    # Retry schedule setup for chats the account cannot access yet
    setup_retry_days: int = 3
    setup_max_attempts: int = 3
    setup_retry_hour: int = 11

    # Non-Premium: no schedule_repeat_period — re-schedule every day before start_hour.
    # Empty / unset → start_hour - 1 (wrapped).
    nonpremium_reschedule_hour: int | None = None

    # Keep Telethon accounts looking active (UpdateStatus online → offline).
    # Interval ~3.5h ± 30m ≈ каждые 3–4 часа.
    online_ping_hours: float = 3.5
    online_ping_jitter_sec: int = 1800
    online_hold_seconds: float = 4.0
    online_ping_enabled: bool = True

    # AI-маркетолог: тихий сбор + утренний брифинг
    marketer_interval_min: int = 30
    marketer_enabled: bool = True
    marketer_morning_hour: int = 7
    marketer_morning_minute: int = 40
    # Цель: пост в группу каждые 5–10 минут (наши аккаунты суммарно).
    density_target_min: float = 5.0
    density_target_max: float = 10.0

    # ИИ-оператор: цикл анализа по фактам (без Telegram-запросов, кроме починки)
    operator_interval_min: int = 30
    # Необязательно: краткий вывод от Claude по уже посчитанным фактам (без ключа — выключено)
    anthropic_api_key: str = ""
    operator_llm_model: str = "claude-sonnet-5-5"
    # SpamBot: проверять каждые N часов (и сразу при подозрении на лимит)
    spam_recheck_hours: float = 3.0

    # Факты отправки / баны и муты / SpamBot / советы по аккаунтам
    facts_minute: int = 7  # каждый час в :07 читаем историю чатов
    facts_hours: float = 3.0  # окно сбора (перекрывается — повторов нет)
    restrictions_scan_every_hours: int = 3
    spam_check_hour: int = 6
    advisor_hour: int = 12
    advisor_mention: str = "@arxixx"
    # важные чаты (подстроки названий): выделяем в отчётах по фактам
    priority_chats: str = "get-chat,lustify"
    # аккаунт «приносит больше вреда, чем пользы»: доля недоступных чатов
    advisor_stop_share: float = 0.6
    advisor_warn_share: float = 0.35

    @property
    def priority_keys(self) -> list[str]:
        return [p.strip().casefold() for p in self.priority_chats.split(",") if p.strip()]

    @property
    def nonpremium_hour(self) -> int:
        if self.nonpremium_reschedule_hour is not None:
            return int(self.nonpremium_reschedule_hour) % 24
        return (int(self.start_hour) - 1) % 24

    @property
    def admins(self) -> set[int]:
        ids: set[int] = set()
        for part in self.admin_ids.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
