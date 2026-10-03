"""Раз в неделю: тихо проверить, не сняли ли бан (без вступления и отправки)."""

from __future__ import annotations

import logging

from aiogram import Bot

from app.notify import safe_send
from app.store import Store
from app.tg.client import telethon_client
from app.tg.resolve import lookup_entity
from app.tg.restrictions import probe_restriction

log = logging.getLogger("marketing.bans")


async def recheck_bans(
    store: Store, bot: Bot | None = None, admin_chat_id: int | None = None
) -> str:
    """Только чтение статуса участника. В забаненные чаты не вступаем и не пишем:
    если бан снят админом, пара возвращается в обычный поток (маркетолог вступит сам)."""
    bans = await store.list_restrictions(kinds=("ban",))
    by_acc: dict[int, list] = {}
    for r in bans:
        by_acc.setdefault(r.account_id, []).append(r)
    lifted: list[str] = []
    for acc_id, items in by_acc.items():
        acc = await store.get_account(acc_id)
        if not acc or not acc.telethon_session:
            continue
        try:
            async with telethon_client(acc.telethon_session) as client:
                for r in items:
                    chat = await store.get_chat(r.chat_pk)
                    if chat is None:
                        continue
                    try:
                        entity = await lookup_entity(client, chat)
                        if entity is None:
                            continue
                        probe = await probe_restriction(client, entity)
                    except Exception:  # noqa: BLE001
                        continue
                    still = probe.kind == "ban" or (
                        probe.kind == "left" and probe.detail.startswith("чат недоступен")
                    )
                    if still or probe.kind in {"unknown", "mute", "nowrite"}:
                        continue
                    if await store.resolve_restriction(acc.id, chat.id):
                        await store.reset_setup_state(acc.id, chat.id)
                        lifted.append(f"{acc.label} → {chat.display_name}")
        except Exception as e:  # noqa: BLE001
            log.info("ban recheck failed for %s: %s", acc.label, e)
    summary = f"Проверка банов: {len(bans)} в базе, снято {len(lifted)}"
    if lifted:
        summary += ": " + "; ".join(lifted[:10])
        if bot and admin_chat_id:
            await safe_send(bot, admin_chat_id, "✅ " + summary)
    return summary
