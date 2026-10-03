"""Плотность наших постов в schedule-чате и советы по аккаунтам."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.models import Account, Chat, MinuteSlot, SetupState


# Цель: наши сообщения в одну группу чаще, чем раз в TARGET_MAX_MIN.
TARGET_MIN_MIN = 5.0
TARGET_MAX_MIN = 10.0


@dataclass
class ChatDensity:
    chat_pk: int
    title: str
    interval_minutes: int
    accounts_live: int
    accounts_planned: int
    avg_gap_min: float
    posts_per_hour: float
    status: str  # ok | thin | critical | empty
    advice: str
    need_accounts: int = 0


@dataclass
class GrowthAdvice:
    densities: list[ChatDensity] = field(default_factory=list)
    thoughts: list[str] = field(default_factory=list)
    buy_accounts: int = 0
    premium_note: str = ""


def avg_gap_minutes(interval_minutes: int, accounts_live: int) -> float:
    """Средний интервал между нашими постами в чате (разные аккаунты)."""
    n = max(0, int(accounts_live))
    if n <= 0:
        return float("inf")
    return float(max(1, int(interval_minutes))) / n


def posts_per_hour(interval_minutes: int, accounts_live: int) -> float:
    n = max(0, int(accounts_live))
    if n <= 0:
        return 0.0
    return n * (60.0 / max(1, int(interval_minutes)))


def accounts_needed_for_gap(
    interval_minutes: int,
    current_live: int,
    target_gap: float = TARGET_MAX_MIN,
) -> int:
    """Сколько аккаунтов нужно, чтобы gap ≤ target_gap."""
    need = math.ceil(max(1, int(interval_minutes)) / max(1.0, float(target_gap)))
    return max(0, need - max(0, int(current_live)))


def analyze_chat_density(
    chat: Chat,
    *,
    live_account_ids: set[int],
    planned_account_ids: set[int],
) -> ChatDensity:
    live_n = len(live_account_ids)
    plan_n = len(planned_account_ids)
    gap = avg_gap_minutes(chat.interval_minutes, live_n)
    pph = posts_per_hour(chat.interval_minutes, live_n)

    if live_n == 0:
        need = accounts_needed_for_gap(chat.interval_minutes, 0, TARGET_MAX_MIN)
        return ChatDensity(
            chat_pk=chat.id,
            title=chat.display_name,
            interval_minutes=chat.interval_minutes,
            accounts_live=0,
            accounts_planned=plan_n,
            avg_gap_min=gap,
            posts_per_hour=0.0,
            status="empty",
            advice=(
                f"Нет живых аккаунтов в «{chat.display_name}». "
                f"Нужно минимум {max(1, need)} акк. для плотности ≤{TARGET_MAX_MIN:g} мин."
            ),
            need_accounts=max(1, need),
        )

    if gap > TARGET_MAX_MIN:
        need = accounts_needed_for_gap(chat.interval_minutes, live_n, TARGET_MAX_MIN)
        status = "critical"
        advice = (
            f"Редко: раз в ~{gap:.1f} мин (цель ≤{TARGET_MAX_MIN:g}). "
            f"Купи ещё {need} акк. → будет ~{avg_gap_minutes(chat.interval_minutes, live_n + need):.1f} мин."
        )
    elif gap > TARGET_MIN_MIN:
        need = accounts_needed_for_gap(chat.interval_minutes, live_n, TARGET_MIN_MIN)
        status = "thin"
        advice = (
            f"Плотность средняя (~{gap:.1f} мин). "
            f"Для цели ≤{TARGET_MIN_MIN:g} мин добавь {need} акк."
            if need
            else f"Плотность ~{gap:.1f} мин — в целевом коридоре {TARGET_MIN_MIN:g}–{TARGET_MAX_MIN:g}."
        )
    else:
        need = 0
        status = "ok"
        advice = (
            f"Плотно: ~{gap:.1f} мин / {pph:.1f} постов·час. "
            f"Ок для трафика; следи за спам-риском."
        )

    if plan_n > live_n:
        advice += f" В плане {plan_n}, факт {live_n} — добей назначение."

    return ChatDensity(
        chat_pk=chat.id,
        title=chat.display_name,
        interval_minutes=chat.interval_minutes,
        accounts_live=live_n,
        accounts_planned=plan_n,
        avg_gap_min=gap if math.isfinite(gap) else 9999.0,
        posts_per_hour=pph,
        status=status,
        advice=advice,
        need_accounts=need,
    )


def build_growth_advice(
    chats: list[Chat],
    accounts: list[Account],
    slots: list[MinuteSlot],
    ok_states: list[SetupState],
) -> GrowthAdvice:
    ok_keys = {(s.account_id, s.chat_pk) for s in ok_states}
    slots_by_chat: dict[int, list[MinuteSlot]] = {}
    for s in slots:
        slots_by_chat.setdefault(s.chat_pk, []).append(s)

    densities: list[ChatDensity] = []
    for chat in chats:
        if not chat.is_schedule or not chat.enabled:
            continue
        planned = {s.account_id for s in slots_by_chat.get(chat.id, [])}
        live = {aid for aid in planned if (aid, chat.id) in ok_keys}
        # также ok без slot (редко)
        for aid, cpk in ok_keys:
            if cpk == chat.id:
                live.add(aid)
        densities.append(
            analyze_chat_density(
                chat, live_account_ids=live, planned_account_ids=planned
            )
        )

    densities.sort(
        key=lambda d: {"critical": 0, "empty": 1, "thin": 2, "ok": 3}.get(d.status, 9)
    )
    buy = max((d.need_accounts for d in densities), default=0)
    # суммарно по «дырявым» чатам — берём max, не сумму (один акк кроет несколько чатов)
    buy_sum_hint = sum(d.need_accounts for d in densities if d.status in {"critical", "empty"})

    premium_n = sum(1 for a in accounts if a.has_premium)
    total = len(accounts)
    thoughts: list[str] = []

    crit = [d for d in densities if d.status in {"critical", "empty"}]
    thin = [d for d in densities if d.status == "thin"]
    if crit:
        thoughts.append(
            f"{len(crit)} чатов с дырой по плотности — трафик теряем. "
            f"Приоритет: докупить аккаунты (ориентир +{buy} шт.)."
        )
    if thin:
        thoughts.append(
            f"{len(thin)} чатов в «средней» зоне (>{TARGET_MIN_MIN:g} мин) — "
            f"можно поджать до {TARGET_MIN_MIN:g} мин ещё аккаунтами."
        )
    if not crit and not thin and densities:
        thoughts.append(
            f"Плотность по {len(densities)} schedule-чатам в норме "
            f"(≤{TARGET_MAX_MIN:g} мин). Дальше — удержание и лиды."
        )

    if total == 0:
        premium_note = "Нет аккаунтов — сначала заведи хотя бы 3–5 под schedule."
    elif premium_n == 0:
        premium_note = (
            "Premium нет: суточный cron переназначает сетку. "
            "1–2 Premium повысят стабильность repeat; плотность растёт от числа аккаунтов, не от Premium."
        )
    elif premium_n == total:
        premium_note = (
            f"Все {total} акк. Premium — надёжно, но дорого. "
            f"Для плотности дешевле микс: 1–2 Premium + обычные. "
            f"По желанию с части сними Premium и купи дополнительные слоты."
        )
    else:
        premium_note = (
            f"Premium {premium_n}/{total}. Оптимум: держать 1–2 Premium для якоря schedule, "
            f"остальной прирост — обычные аккаунты (дешевле за ту же плотность)."
        )

    thoughts.append(premium_note)
    if buy_sum_hint > buy:
        thoughts.append(
            f"Если закрывать каждый чат отдельно — до +{buy_sum_hint} слотов; "
            f"практичнее +{buy} универсальных аккаунтов на все thin/critical."
        )
    thoughts.append(
        "На горизонт: больше аккаунтов → чаще пост в группе → больше касаний → больше лидов в ЛС. "
        "Параллельно чинить баны/пустые очереди, иначе новые аккаунты не дадут факта."
    )

    return GrowthAdvice(
        densities=densities,
        thoughts=thoughts[:10],
        buy_accounts=buy,
        premium_note=premium_note,
    )
