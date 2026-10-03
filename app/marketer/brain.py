"""Маркетолог = ИИ-оператор: цикл по фактам (см. app/operator) + уведомления.

Прежняя версия оценивала «факт» по статусу настройки и по очереди отложенных сообщений,
каждые 20 минут заново пробовала недоступные пары и держала Telethon-сессии (из-за чего
суточная пересборка шла часами). Теперь цикл работает по базе фактов и без лишних
обращений к Telegram.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.notify import safe_send
from app.operator.brief import build_brief
from app.operator.engine import ActionResult, CycleResult, Escalation, run_cycle
from app.store import Store
from app.utils.timefmt import parse_utc, to_iso

log = logging.getLogger("marketing.marketer")

NOTIFY_SEVERITIES = {"critical", "high"}
REPEAT_HOURS = 24


@dataclass
class MarketerResult:
    cycle: CycleResult | None = None
    digest: str = ""
    new_escalations: list[Escalation] = field(default_factory=list)
    actions: list[ActionResult] = field(default_factory=list)

    @property
    def pairs(self) -> int:
        return self.cycle.pairs if self.cycle else 0

    @property
    def working(self) -> int:
        return self.cycle.working if self.cycle else 0


async def _fresh_escalations(store: Store, items: list[Escalation]) -> list[Escalation]:
    """Не повторяем одно и то же чаще раза в сутки."""
    now = datetime.now(timezone.utc)
    fresh: list[Escalation] = []
    for e in items:
        if e.severity not in NOTIFY_SEVERITIES:
            continue
        last = parse_utc(await store.get_setting(f"op:esc:{e.key}", ""))
        if last is None or (now - last) >= timedelta(hours=REPEAT_HOURS):
            fresh.append(e)
    return fresh


async def run_marketer_once(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    notify: bool = False,
    morning: bool = False,
    account_ids: list[int] | None = None,
) -> MarketerResult:
    enabled = (await store.get_setting("marketer_enabled", "1")) == "1"
    if not enabled and not morning:
        return MarketerResult(digest="Оператор выключен")

    cycle = await run_cycle(store, bot, admin_chat_id, act=True)
    result = MarketerResult(cycle=cycle, actions=list(cycle.actions))
    now_iso = to_iso(datetime.now(timezone.utc))

    if morning:
        result.digest = await build_brief(store, cycle, morning=True)
        if bot and admin_chat_id:
            await safe_send(bot, admin_chat_id, result.digest, parse_mode="HTML")
            await store.set_setting("marketer_last_digest_at", now_iso)
        for e in cycle.escalations:
            await store.set_setting(f"op:esc:{e.key}", now_iso)
        return result

    result.new_escalations = await _fresh_escalations(store, cycle.escalations)
    if result.new_escalations:
        result.digest = await build_brief(
            store, cycle, morning=False, only_new=result.new_escalations
        )
        if bot and admin_chat_id:
            await safe_send(bot, admin_chat_id, result.digest, parse_mode="HTML")
            await store.set_setting("marketer_last_digest_at", now_iso)
        for e in result.new_escalations:
            await store.set_setting(f"op:esc:{e.key}", now_iso)
    return result
