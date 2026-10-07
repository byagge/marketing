"""База банов: фиксация, уведомление админа, отчёт «добавлять ли новый аккаунт»."""

from __future__ import annotations

import logging
from collections import defaultdict
from html import escape

from aiogram import Bot

from app.config import get_settings
from app.models import Account, Chat, ChatBan
from app.store import Store

log = logging.getLogger("marketing.bans")

REASON_TEXT = {
    "join_ban": "не пускает (бан на вступлении)",
    "removed": "вылетел / исключён из чата",
    "send_ban": "бан на отправку сообщений",
}

# Если забанено не меньше этой доли аккаунтов чата — это системная проблема чата,
# а не «не повезло одному аккаунту».
MASS_BAN_RATIO = 0.4


def admin_target() -> int | None:
    return next(iter(sorted(get_settings().admins)), None)


def ban_advice(banned_n: int, accounts_n: int) -> str:
    """Короткая рекомендация по чату: добавлять ли новый аккаунт."""
    if accounts_n <= 0:
        return "нет аккаунтов для оценки"
    ratio = banned_n / accounts_n
    if banned_n >= 3 and ratio >= MASS_BAN_RATIO:
        return (
            "массовые баны — новый аккаунт, скорее всего, тоже улетит; "
            "сначала смените текст/частоту или исключите чат"
        )
    if banned_n >= 2:
        return "несколько банов — проверьте текст и частоту, новый аккаунт можно пробовать осторожно"
    return "точечный бан — замену аккаунтом ставить можно"


def format_ban_line(ban: ChatBan) -> str:
    reason = REASON_TEXT.get(ban.reason, ban.reason or "бан")
    when = (ban.detected_at or "")[:16].replace("T", " ")
    return f"{ban.account_label or ban.account_id} → «{ban.chat_title or ban.chat_pk}»: {reason} ({when} UTC)"


async def register_ban(
    store: Store,
    account: Account,
    chat: Chat,
    reason: str,
    detail: str = "",
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    notify: bool = True,
) -> tuple[ChatBan, bool]:
    """
    Занести бан в базу и (при notify) один раз написать админу.
    Возвращает (бан, is_new).
    """
    ban, is_new = await store.record_ban(account.id, chat.id, reason, detail)
    log.warning("ban: %s -> %s (%s) %s", account.label, chat.title, reason, detail)
    if is_new and notify:
        target = admin_chat_id or admin_target()
        if bot is not None and target:
            try:
                await bot.send_message(target, await format_ban_notice(store, [ban]))
                await store.mark_ban_notified([ban.id])
            except Exception:
                log.exception("не удалось отправить уведомление о бане")
    return ban, is_new


async def format_ban_notice(store: Store, bans: list[ChatBan]) -> str:
    """Текст уведомления о новых банах с рекомендацией по каждому чату."""
    accounts_n = len([a for a in await store.list_accounts() if a.telethon_session])
    by_chat: dict[int, list[ChatBan]] = defaultdict(list)
    for b in bans:
        by_chat[b.chat_pk].append(b)
    all_active = await store.list_bans(active_only=True)
    totals: dict[int, int] = defaultdict(int)
    for b in all_active:
        totals[b.chat_pk] += 1
    lines = [f"🚫 Новые баны: {len(bans)}"]
    for chat_pk, items in by_chat.items():
        title = items[0].chat_title or str(chat_pk)
        lines.append(f"\n«{title}»")
        for b in items[:15]:
            reason = REASON_TEXT.get(b.reason, b.reason or "бан")
            lines.append(f"· {b.account_label or b.account_id}: {reason}")
        if len(items) > 15:
            lines.append(f"· … ещё {len(items) - 15}")
        lines.append(f"Итого забанено в чате: {totals.get(chat_pk, len(items))} из {accounts_n}")
        lines.append(f"Рекомендация: {ban_advice(totals.get(chat_pk, len(items)), accounts_n)}")
    lines.append("\nЗанесено в базу банов (меню → Баны). Автопилот эти пары больше не трогает.")
    return "\n".join(lines)


async def ban_report_html(store: Store) -> str:
    """Отчёт по базе банов, сгруппированный по чатам."""
    bans = await store.list_bans(active_only=True)
    accounts_n = len([a for a in await store.list_accounts() if a.telethon_session])
    if not bans:
        return "🚫 <b>База банов</b>\n\nАктивных банов нет."
    by_chat: dict[str, list[ChatBan]] = defaultdict(list)
    for b in bans:
        by_chat[b.chat_title or str(b.chat_pk)].append(b)
    lines = [f"🚫 <b>База банов</b> — активных: {len(bans)} в {len(by_chat)} чатах\n"]
    for title, items in sorted(by_chat.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"<b>{escape(title)}</b> — {len(items)} из {accounts_n}")
        lines.append(f"<i>{escape(ban_advice(len(items), accounts_n))}</i>")
        for b in items[:12]:
            reason = REASON_TEXT.get(b.reason, b.reason or "бан")
            lines.append(f"· {escape(b.account_label or str(b.account_id))}: {escape(reason)}")
        if len(items) > 12:
            lines.append(f"· … ещё {len(items) - 12}")
        lines.append("")
    return "\n".join(lines).rstrip()
