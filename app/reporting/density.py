"""Плотность наших постов в schedule-чате и рекомендации по аккаунтам."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.models import Account, Chat, MinuteSlot, SetupState


@dataclass
class ChatDensity:
    chat_pk: int
    title: str
    interval_minutes: int
    accounts_live: int
    accounts_planned: int
    avg_gap_min: float
    status: str  # ok | sparse | dense | empty
    advice: str
    accounts_needed_for_10m: int = 0
    accounts_needed_for_5m: int = 0


@dataclass
class DensityReport:
    chats: list[ChatDensity]
    sparse_n: int
    ok_n: int
    dense_n: int
    global_advice: list[str]


def avg_gap_minutes(interval_minutes: int, live_accounts: int) -> float:
    """Средний интервал между нашими постами в одной группе.

    Каждый живой аккаунт шлёт раз в ``interval_minutes``.
    Вместе: пост roughly каждые interval/n минут.
    """
    n = max(0, int(live_accounts))
    if n <= 0:
        return float("inf")
    return max(0.1, float(interval_minutes) / n)


def gap_from_minutes(minutes: list[int], interval_minutes: int) -> float | None:
    """Уточнение по фактическим start_minute внутри часа (равномерность)."""
    if len(minutes) < 2:
        return None
    mins = sorted(int(m) % 60 for m in minutes)
    gaps = [mins[i + 1] - mins[i] for i in range(len(mins) - 1)]
    # wrap around hour
    gaps.append(60 - mins[-1] + mins[0])
    # В одном часе каждый аккаунт обычно 1 старт; реальный шаг ≈ mean(gaps)
    # но полный цикл = interval, поэтому комбинируем.
    mean_phase = sum(gaps) / len(gaps)
    # Эффективный gap между постами ≈ interval / n, фаза только проверяет равномерность
    return mean_phase * (interval_minutes / 60.0) if interval_minutes >= 60 else mean_phase


def classify_gap(avg_gap: float, *, target_min: float = 5.0, target_max: float = 10.0) -> str:
    if avg_gap == float("inf") or avg_gap <= 0:
        return "empty"
    if avg_gap > target_max:
        return "sparse"
    if avg_gap < target_min:
        return "dense"
    return "ok"


def accounts_to_reach(interval_minutes: int, target_gap: float, have: int) -> int:
    need_total = max(1, math.ceil(float(interval_minutes) / max(0.5, target_gap)))
    return max(0, need_total - max(0, have))


def build_density_report(
    chats: list[Chat],
    accounts: list[Account],
    slots: list[MinuteSlot],
    ok_states: list[SetupState],
    *,
    target_min: float = 5.0,
    target_max: float = 10.0,
) -> DensityReport:
    ok_keys = {(s.account_id, s.chat_pk) for s in ok_states if s.status == "ok"}
    premium_n = sum(1 for a in accounts if a.has_premium)
    nonprem_n = len(accounts) - premium_n

    rows: list[ChatDensity] = []
    for chat in chats:
        if not chat.is_schedule or not chat.enabled:
            continue
        planned = [s for s in slots if s.chat_pk == chat.id]
        live = [s for s in planned if (s.account_id, s.chat_pk) in ok_keys]
        n_live = len(live)
        n_plan = len(planned)
        gap = avg_gap_minutes(chat.interval_minutes, n_live)
        status = classify_gap(gap, target_min=target_min, target_max=target_max)

        need_10 = accounts_to_reach(chat.interval_minutes, target_max, n_live)
        need_5 = accounts_to_reach(chat.interval_minutes, target_min, n_live)

        if status == "empty":
            advice = (
                f"Нет живых аккаунтов в «{chat.display_name}». "
                f"Сначала назначить schedule (setup), потом наращивать плотность."
            )
        elif status == "sparse":
            advice = (
                f"Пост раз в ~{gap:.0f} мин (цель {target_min:.0f}–{target_max:.0f}). "
                f"Не хватает ~{need_10} акк. до шага {target_max:.0f} мин "
                f"(или ~{need_5} до {target_min:.0f} мин). "
                f"Имеет смысл докупить обычные (без Premium) — быстрее и дешевле "
                f"для плотности, Premium оставить на ключевые чаты."
            )
        elif status == "dense":
            advice = (
                f"Слишком плотно: ~{gap:.1f} мин между постами "
                f"(риск спам-реакции модерации). "
                f"Можно убрать лишние аккаунты из чата или не гонять Premium "
                f"на все слоты — сэкономить и снизить давление."
            )
        else:
            advice = (
                f"Норма: ~{gap:.1f} мин между нашими постами "
                f"({n_live} живых акк., интервал сетки {chat.interval_minutes})."
            )

        rows.append(
            ChatDensity(
                chat_pk=chat.id,
                title=chat.display_name,
                interval_minutes=chat.interval_minutes,
                accounts_live=n_live,
                accounts_planned=n_plan,
                avg_gap_min=gap if gap != float("inf") else 0.0,
                status=status,
                advice=advice,
                accounts_needed_for_10m=need_10,
                accounts_needed_for_5m=need_5,
            )
        )

    sparse = [r for r in rows if r.status in {"sparse", "empty"}]
    dense = [r for r in rows if r.status == "dense"]
    ok = [r for r in rows if r.status == "ok"]

    global_advice: list[str] = []
    if sparse:
        total_need = max((r.accounts_needed_for_10m for r in sparse), default=0)
        global_advice.append(
            f"{len(sparse)} чатов с редкой подачей. "
            f"Чтобы везде было ≤{target_max:.0f} мин — ориентир +{total_need} аккаунтов "
            f"(лучше без Premium: дешевле масштабировать плотность)."
        )
    if dense:
        global_advice.append(
            f"{len(dense)} чатов перегреты (<{target_min:.0f} мин). "
            f"Снимите часть аккаунтов или Premium с второстепенных — меньше банов, те же лиды."
        )
    if premium_n and sparse:
        global_advice.append(
            f"Сейчас Premium: {premium_n}, обычных: {nonprem_n}. "
            f"Для роста трафика выгоднее докупить ordinary-аккаунты под плотность, "
            f"а Premium держать там, где критичен суточный repeat без cron."
        )
    if not sparse and not dense and rows:
        global_advice.append(
            "Плотность по schedule в целевом коридоре 5–10 мин. "
            "Дальше рост трафика — новые чаты + те же аккаунты, либо ещё аккаунты под новые группы."
        )
    if not rows:
        global_advice.append("Нет schedule-чатов — нечего измерять.")

    return DensityReport(
        chats=rows,
        sparse_n=len(sparse),
        ok_n=len(ok),
        dense_n=len(dense),
        global_advice=global_advice,
    )
