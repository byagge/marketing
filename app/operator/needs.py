"""Сколько новых аккаунтов нужно, чтобы в каждый чат писали не реже раза в N минут.

Считаем честно: в «есть» входят только аккаунты, которые реально могут писать в чат
сейчас или после автопочинки (вступить, настроить, пересоздать очередь, дождаться конца
мута). Бан, SpamBlock, выключенные вручную и аккаунты, которые не могут вступить
(спамблок при публичном чате, «сдался», нет ссылки), не считаются.

Один аккаунт работает во ВСЕХ чатах одновременно (в каждом — раз в interval минут), поэтому
под каждый чат отдельный аккаунт не нужен: новые аккаунты вступают сразу во все чаты, и
нужно столько, сколько не хватает самому «голодному» чату (максимум дефицитов, не сумма).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from app.utils.coverage import NEED_INTERVAL_CAP

from app.operator.diagnose import (
    MUTED,
    NO_DATA,
    NOT_MEMBER,
    NOT_MEMBER_NO_LINK,
    NOT_SCHEDULED,
    SILENT,
    SPAMBLOCKED,
    WARMING,
    WORKING,
    Diagnosis,
    PairFacts,
)

TARGET_GAP_MIN = 5.0

# коды joinwhy, при которых вступить реально без людей (иначе аккаунт «застрял»)
_JOINABLE = {"", "ready", "wait", "request"}

_ALWAYS = {WORKING, WARMING, NOT_SCHEDULED, SILENT, NO_DATA}


@dataclass
class ChatNeed:
    chat_pk: int
    title: str
    priority: bool
    interval: int
    needed: int
    eligible: int
    returning: int = 0  # замьючены на известный срок — вернутся сами
    stuck: int = 0  # не в чате и вступить сейчас не могут (спамблок / сдался / нет ссылки)

    @property
    def deficit(self) -> int:
        return max(0, self.needed - self.eligible)

    @property
    def deficit_if_returning(self) -> int:
        """Дефицит, если все замьюченные на известный срок вернутся."""
        return max(0, self.needed - self.eligible - self.returning)


def compute_needs(
    diagnoses: list[tuple[PairFacts, Diagnosis]],
    priority_keys: list[str],
    target_gap: float = TARGET_GAP_MIN,
) -> list[ChatNeed]:
    eligible: dict[int, int] = defaultdict(int)
    returning: dict[int, int] = defaultdict(int)
    stuck: dict[int, int] = defaultdict(int)
    meta: dict[int, PairFacts] = {}
    for f, d in diagnoses:
        if f.chat_kind != "schedule":
            continue
        meta.setdefault(f.chat_pk, f)
        if d.cause == NOT_MEMBER:
            # «вступит сам» верно, только если вступить реально можно
            if f.join_code in _JOINABLE:
                eligible[f.chat_pk] += 1
            else:
                stuck[f.chat_pk] += 1
            continue
        if d.cause == NOT_MEMBER_NO_LINK:
            stuck[f.chat_pk] += 1
            continue
        ok = d.cause in _ALWAYS or (d.cause in {MUTED, SPAMBLOCKED} and f.restriction_expired)
        if ok:
            eligible[f.chat_pk] += 1
        elif d.cause in {MUTED, SPAMBLOCKED} and f.restriction_until:
            returning[f.chat_pk] += 1
    out: list[ChatNeed] = []
    for pk, f in meta.items():
        capped_interval = min(max(1, f.interval), NEED_INTERVAL_CAP)
        needed = max(1, math.ceil(capped_interval / max(0.5, target_gap)))
        title = f.chat_title
        out.append(
            ChatNeed(
                chat_pk=pk,
                title=title,
                priority=any(k in title.casefold() for k in priority_keys),
                interval=f.interval,
                needed=needed,
                eligible=eligible.get(pk, 0),
                returning=returning.get(pk, 0),
                stuck=stuck.get(pk, 0),
            )
        )
    out.sort(key=lambda n: (-n.deficit, not n.priority, n.title.casefold()))
    return out


def accounts_to_buy(needs: list[ChatNeed]) -> int:
    """Сколько новых аккаунтов нужно: максимум дефицитов по чатам (не сумма)."""
    return max((n.deficit for n in needs), default=0)


def accounts_to_buy_if_returning(needs: list[ChatNeed]) -> int:
    """То же, но если все замьюченные на известный срок вернутся."""
    return max((n.deficit_if_returning for n in needs), default=0)
