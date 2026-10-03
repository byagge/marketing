"""Сколько новых аккаунтов нужно, чтобы в каждый чат писали не реже раза в N минут.

Считаем честно: в «есть» входят только аккаунты, которые реально могут писать в чат
сейчас или после автопочинки (вступить, настроить, пересоздать очередь, дождаться конца
мута). Бан, SpamBlock, выключенные вручную и аккаунты без способа вступить не считаются.
Новые аккаунты вступают сразу во все чаты, поэтому нужно ≈ максимальный дефицит по чатам.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from app.operator.diagnose import (
    MUTED,
    NO_DATA,
    NOT_MEMBER,
    NOT_SCHEDULED,
    SILENT,
    SPAMBLOCKED,
    WARMING,
    WORKING,
    Diagnosis,
    PairFacts,
)

TARGET_GAP_MIN = 5.0

_ALWAYS = {WORKING, WARMING, NOT_MEMBER, NOT_SCHEDULED, SILENT, NO_DATA}


@dataclass
class ChatNeed:
    chat_pk: int
    title: str
    priority: bool
    interval: int
    needed: int
    eligible: int

    @property
    def deficit(self) -> int:
        return max(0, self.needed - self.eligible)


def compute_needs(
    diagnoses: list[tuple[PairFacts, Diagnosis]],
    priority_keys: list[str],
    target_gap: float = TARGET_GAP_MIN,
) -> list[ChatNeed]:
    eligible: dict[int, int] = defaultdict(int)
    meta: dict[int, PairFacts] = {}
    for f, d in diagnoses:
        if f.chat_kind != "schedule":
            continue
        meta.setdefault(f.chat_pk, f)
        ok = d.cause in _ALWAYS or (d.cause in {MUTED, SPAMBLOCKED} and f.restriction_expired)
        if ok:
            eligible[f.chat_pk] += 1
    out: list[ChatNeed] = []
    for pk, f in meta.items():
        needed = max(1, math.ceil(max(1, f.interval) / max(0.5, target_gap)))
        title = f.chat_title
        out.append(
            ChatNeed(
                chat_pk=pk,
                title=title,
                priority=any(k in title.casefold() for k in priority_keys),
                interval=f.interval,
                needed=needed,
                eligible=eligible.get(pk, 0),
            )
        )
    out.sort(key=lambda n: (-n.deficit, not n.priority, n.title.casefold()))
    return out


def accounts_to_buy(needs: list[ChatNeed]) -> int:
    return max((n.deficit for n in needs), default=0)
