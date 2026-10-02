"""Частота отправки в чат и сколько аккаунтов не хватает до цели (по умолчанию 5 мин)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

TARGET_GAP_MIN = 5


def max_gap(minutes: list[int], period: int = 60) -> int:
    """Максимальная пауза между отправками по кругу часа (минуты слотов)."""
    pts = sorted(set(int(m) % period for m in minutes))
    if not pts:
        return period
    if len(pts) == 1:
        return period
    gaps = [b - a for a, b in zip(pts, pts[1:])]
    gaps.append(pts[0] + period - pts[-1])
    return max(gaps)


def accounts_needed(interval_minutes: int, target_gap: int = TARGET_GAP_MIN) -> int:
    """Сколько аккаунтов нужно, чтобы чат получал сообщение не реже раза в target_gap мин.

    Каждый аккаунт пишет раз в interval минут → нужно ceil(interval / gap).
    """
    return max(1, math.ceil(max(1, int(interval_minutes)) / max(1, target_gap)))


@dataclass
class ChatCoverage:
    chat_pk: int
    title: str
    interval: int
    working: list[str] = field(default_factory=list)
    banned: list[str] = field(default_factory=list)
    muted: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)  # не настроен / чат не найден
    gap: int = 0
    needed: int = 0
    sent_24h: int = 0
    candidates: list[str] = field(default_factory=list)
    candidates_join: list[str] = field(default_factory=list)

    @property
    def deficit(self) -> int:
        return max(0, self.needed - len(self.working))

    @property
    def avg_step(self) -> float:
        n = len(self.working)
        return (self.interval / n) if n else float("inf")

    @property
    def ok(self) -> bool:
        return self.deficit == 0 and self.gap <= TARGET_GAP_MIN * 2


def render_line(c: ChatCoverage) -> str:
    step = f"{c.avg_step:.1f}" if c.working else "—"
    line = (
        f"{c.title}: работает {len(c.working)}/{c.needed} "
        f"(шаг ≈{step} мин, макс. пауза {c.gap} мин, за 24ч отправок {c.sent_24h})"
    )
    if c.deficit:
        line += f"\n   нужно ещё {c.deficit} акк."
        if c.candidates:
            line += f"; можно подключить: {', '.join(c.candidates[:8])}"
        if c.candidates_join:
            line += f"; сначала вступить: {', '.join(c.candidates_join[:8])}"
    blocked = []
    if c.banned:
        blocked.append(f"бан: {', '.join(c.banned)}")
    if c.muted:
        blocked.append(f"мут: {', '.join(c.muted)}")
    if c.disabled:
        blocked.append(f"выкл: {', '.join(c.disabled)}")
    if c.broken:
        blocked.append(f"не настроен: {', '.join(c.broken[:8])}")
    if blocked:
        line += "\n   " + " | ".join(blocked)
    return line
