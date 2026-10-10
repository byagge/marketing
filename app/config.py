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

    # Без Premium (нет repeat): держим расписание на столько часов дальше суток, чтобы
    # опоздавшая ночная пересборка не оставляла чат без отправок.
    nonpremium_extra_hours: float = 6.0

    # Dead account (расходник): пост во все доступные чаты максимально часто.
    # Минимум ≈14 мин, иначе 99 scheduled-слотов не покрывают сутки.
    dead_interval_minutes: int = 15

    # Перераспределение слотов по фактически работающим аккаунтам.
    auto_rebalance: bool = True
    rebalance_every_hours: float = 3.0
    # перекос: макс. пауза > ideal * factor (и > ideal + 2 мин) → раздвигаем
    rebalance_gap_factor: float = 1.5
    # факты (членство/права/отправки) старше этого считаем устаревшими → «unknown»
    facts_max_age_hours: float = 30.0
    # перед перераспределением не пересобираем факты, если им меньше N минут
    facts_fresh_minutes: int = 120
    facts_chat_pause_sec: float = 0.3
    # настроено давно, но за сутки 0 отправок → «молчит»
    silent_after_hours: float = 26.0

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

    # Советник: «польза против вреда» по аккаунту (см. app/jobs/advisor.py).
    # Спамблок + активный sender → сразу dead (sender выключен, остаётся schedule).
    spam_auto_dead: bool = True
    # «Отключить» — только если пользы нет (клиентов 0, отправок нет) и вред ≥ порога.
    advisor_min_harm: float = 4.0
    # Автопилот: через сколько часов пробовать вступить заново после «сдался».
    autopilot_abandoned_retry_hours: float = 72.0

    # Автопилот: сам вступает в чаты, настраивает отправку, фиксирует баны.
    autopilot_interval_min: int = 60
    # Не более N вступлений на аккаунт за один проход (анти-Flood).
    autopilot_join_per_tick: int = 3
    # Сколько аккаунтов автопилот ведёт одновременно (раньше жёстко 4: проход по 32 акк.
    # занимал больше часа и накладывался на следующий) и сколько минут даём одному аккаунту:
    # зависший аккаунт не должен держать весь проход.
    autopilot_parallel: int = 6
    autopilot_account_timeout_min: float = 30.0
    autopilot_join_pause_min_sec: float = 20.0
    autopilot_join_pause_max_sec: float = 45.0
    autopilot_retry_hours: float = 6.0
    autopilot_max_join_attempts: int = 5
    # Не чаще раза в N часов перенастраивать sender аккаунта «по подозрению».
    autopilot_sender_refresh_hours: float = 6.0

    # Спамблок (@SpamBot): проверка раз в N часов; нагрузка sender падает ступенями
    # 50% сразу → 25% через ramp_1 часов → 0% (стоп sender) через ramp_2 часов.
    # После снятия — recovery часов на 50%, потом 100%. dead_strikes повторов → dead.
    spam_check_hours: float = 3.0
    spam_ramp_hours_1: float = 6.0
    spam_ramp_hours_2: float = 24.0
    spam_recovery_hours: float = 6.0
    spam_dead_strikes: int = 3
    spam_strike_decay_days: int = 30

    # Результативность аккаунтов: сколько людей пишут в личку за окно. Мало/ноль — в список
    # «аккаунты для переоформления» и сообщение админу с mention.
    perf_window_days: int = 7
    perf_min_age_days: int = 7  # новые аккаунты не оцениваем
    perf_low_threshold: int = 3  # меньше стольких людей за окно = «пишут мало»
    perf_check_hours: float = 12.0
    perf_grace_days: int = 7  # после «переоформил» не оцениваем столько дней
    perf_renotify_days: int = 7  # напоминание, если аккаунт всё ещё в списке
    perf_max_dialogs: int = 400
    notify_mention: str = "@arxixx"

    # «Ворота подписки»: сканируем чат на сообщение бота с кнопками-ссылками.
    gate_checks_per_tick: int = 6
    gate_recheck_hours: float = 12.0

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
