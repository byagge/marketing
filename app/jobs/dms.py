"""Люди, которые пишут аккаунтам в личку: реальные данные из диалогов Telegram.

Считаем только людей (боты, «Избранное», служебный 777000 пропускаем).
«Написали первыми» — первое сообщение в диалоге входящее; если первым написал аккаунт
(исходящее), человек учитывается как «ответил», но не как лид.

Диалоги обрабатываем инкрементально (по id последнего сообщения) и с бюджетом на запуск,
чтобы не ловить FloodWait: большая история индексируется за несколько часовых проходов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.models import Account
from app.store import Store
from app.utils.timefmt import to_iso
from app.utils.errfmt import short_error

log = logging.getLogger("marketing.dms")

DEEP_BUDGET = 60  # диалогов с чтением истории за один проход аккаунта
MSG_LIMIT = 100  # сообщений на диалог за раз
TELEGRAM_SERVICE_ID = 777000


@dataclass
class DmStats:
    dialogs: int = 0
    processed: int = 0
    pending: int = 0  # осталось обработать (не хватило бюджета)
    new_people: int = 0
    error: str = ""


def _is_person(entity: Any) -> bool:
    if getattr(entity, "bot", False) or getattr(entity, "is_self", False):
        return False
    if getattr(entity, "deleted", False):
        return False
    uid = int(getattr(entity, "id", 0) or 0)
    return bool(uid) and uid != TELEGRAM_SERVICE_ID


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _name(entity: Any) -> str:
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    return f"{first} {last}".strip()[:80]


async def collect_dms(
    store: Store, client: Any, account: Account, *, budget: int = DEEP_BUDGET
) -> DmStats:
    stats = DmStats()
    tz = get_settings().tz
    known = await store.dm_known(account.id)
    deep = 0
    try:
        async for dialog in client.iter_dialogs(limit=None):
            if not getattr(dialog, "is_user", False):
                continue
            entity = dialog.entity
            if not _is_person(entity):
                continue
            stats.dialogs += 1
            uid = int(entity.id)
            last = getattr(dialog, "message", None)
            last_id = int(getattr(last, "id", 0) or 0)
            last_at = _utc(getattr(last, "date", None))
            unread = int(getattr(dialog, "unread_count", 0) or 0)
            prev = known.get(uid)
            if prev is not None and last_id and prev[0] >= last_id:
                # ничего нового — обновим только «ждёт ответа»
                await store.upsert_dm_person(account.id, uid, unread=unread, last_msg_id=prev[0])
                continue
            if deep >= budget:
                stats.pending += 1
                continue
            deep += 1

            min_id = prev[0] if prev is not None else 0
            incoming: list[Any] = []
            try:
                async for msg in client.iter_messages(entity, limit=MSG_LIMIT, min_id=min_id):
                    if not getattr(msg, "out", False):
                        incoming.append(msg)
            except Exception as e:  # noqa: BLE001
                log.info("dm history failed %s/%s: %s", account.label, uid, e)
                continue

            started_by = None
            first_at = None
            if prev is None:
                # кто написал первым: самое первое сообщение диалога
                try:
                    first_msgs = await client.get_messages(entity, limit=1, reverse=True, min_id=0)
                    first = first_msgs[0] if first_msgs else None
                except Exception:  # noqa: BLE001
                    first = None
                if first is not None:
                    started_by = "us" if getattr(first, "out", False) else "them"
                    first_at = to_iso(_utc(first.date)) if getattr(first, "date", None) else None
                if started_by is None:
                    started_by = "unknown"

            days: set[str] = set()
            newest_in: datetime | None = None
            for m in incoming:
                d = _utc(getattr(m, "date", None))
                if d is None:
                    continue
                days.add(d.astimezone(tz).date().isoformat())
                if newest_in is None or d > newest_in:
                    newest_in = d
            await store.upsert_dm_person(
                account.id,
                uid,
                username=(getattr(entity, "username", None) or "")[:64],
                name=_name(entity),
                started_by=started_by,
                first_at=first_at,
                last_in_at=to_iso(newest_in) if newest_in else None,
                last_msg_id=last_id,
                last_msg_at=to_iso(last_at) if last_at else "",
                add_in=len(incoming),
                unread=unread,
            )
            await store.add_dm_activity(account.id, uid, days)
            stats.processed += 1
            if prev is None and started_by == "them":
                stats.new_people += 1
    except Exception as e:  # noqa: BLE001
        stats.error = f"{short_error(e)}"
        log.warning("dm scan failed for %s: %s", account.label, stats.error)
    await store.set_setting(f"dm_pending:{account.id}", str(stats.pending))
    await store.set_setting(f"dm_scanned_at:{account.id}", to_iso(datetime.now(timezone.utc)))
    return stats
