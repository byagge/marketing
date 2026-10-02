"""Баны / муты, SpamBot, частота, советы по аккаунтам, вступление в недостающие."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.bot.keyboards import MenuCB, confirm_kb, home_row, ib, nav_row
from app.bot.render import safe_edit
from app.context import ctx
from app.jobs import runtime
from app.jobs.advisor import build_advice, run_advisor
from app.jobs.coverage import coverage_report
from app.jobs.join import run_join_missing_all
from app.jobs.spam import STATUS_LABEL, check_account_spam, run_spam_check
from app.notify import safe_send
from app.ui.emoji import pe
from app.ui.screens import prompt_html
from app.utils.timefmt import fmt_local, fmt_until

router = Router()

BANS_PER_PAGE = 18
MUTES_PER_PAGE = 7


def _kb(rows: list[list]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def restr_home_html() -> str:
    bans = await ctx.store.list_restrictions(kinds=("ban",))
    mutes = await ctx.store.list_restrictions(kinds=("mute", "nowrite"))
    accounts = [a for a in await ctx.store.list_accounts() if a.telethon_session]
    limited = sum(1 for a in accounts if a.is_spam_limited)
    unchecked = sum(1 for a in accounts if not a.spam_status)
    return (
        f"{pe('shield')} <b>Баны, муты, SpamBot</b>\n\n"
        f"{pe('block')} Баны: <b>{len(bans)}</b> — в эти чаты больше не вступаем и не пишем\n"
        f"{pe('clock')} Муты / запрет писать: <b>{len(mutes)}</b> — "
        f"срок и причина сохраняются, после окончания пара снова включается\n"
        f"{pe('warn')} С ограничением @SpamBot: <b>{limited}</b> "
        f"(не проверено: {unchecked})\n\n"
        f"Скан (баны/муты + факты отправки) идёт каждый час сам."
    )


def restr_home_kb() -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                ib("Баны", "restr_bans", icon="block"),
                ib("Муты", "restr_mutes", icon="clock"),
            ],
            [ib("По аккаунтам", "restr_acc", icon="user")],
            [
                ib("Скан сейчас", "fact_now", icon="search"),
                ib("SpamBot все", "spam_all", icon="shield"),
            ],
            [
                ib("Частота / сколько акк. добавить", "cov", icon="chart"),
                ib("Советы", "adv", icon="mega"),
            ],
            [ib("Вступить в недостающие (все акк.)", "join_miss_all", icon="users")],
            home_row(),
        ]
    )


@router.callback_query(MenuCB.filter(F.a == "restr"))
async def cb_restr(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(query, await restr_home_html(), restr_home_kb())


def _page(items: list, page: int, per: int) -> tuple[list, int, int]:
    total = max(1, (len(items) + per - 1) // per)
    page = max(0, min(page, total - 1))
    return items[page * per : page * per + per], page, total


@router.callback_query(MenuCB.filter(F.a == "restr_bans"))
async def cb_bans(query: CallbackQuery, callback_data: MenuCB) -> None:
    bans = await ctx.store.list_restrictions(kinds=("ban",))
    chunk, page, total = _page(bans, callback_data.p, BANS_PER_PAGE)
    lines = [f"{pe('block')} <b>Баны</b> ({len(bans)})", ""]
    if not bans:
        lines.append("<i>Банов нет.</i>")
    last_acc = None
    for r in chunk:
        if r.account_label != last_acc:
            lines.append(f"\n{pe('user')} <b>{escape(r.account_label)}</b>")
            last_acc = r.account_label
        until = f" до {fmt_until(r.until_at)}" if r.until_at else ""
        lines.append(
            f"· {escape(r.chat_title)} — с {fmt_local(r.detected_at)}{until}"
        )
    rows = []
    if total > 1:
        rows.append(nav_row("restr_bans", page, total))
    rows.append([ib("Баны/муты", "restr", icon="shield")])
    rows.append(home_row())
    await safe_edit(query, "\n".join(lines)[:3900], _kb(rows))


@router.callback_query(MenuCB.filter(F.a == "restr_mutes"))
async def cb_mutes(query: CallbackQuery, callback_data: MenuCB) -> None:
    mutes = await ctx.store.list_restrictions(kinds=("mute", "nowrite"))
    # скоро заканчивающиеся — выше
    mutes.sort(key=lambda r: (r.until_at or "9999", r.account_label.casefold()))
    chunk, page, total = _page(mutes, callback_data.p, MUTES_PER_PAGE)
    lines = [f"{pe('clock')} <b>Муты / запрет писать</b> ({len(mutes)})", ""]
    if not mutes:
        lines.append("<i>Мутов нет.</i>")
    for r in chunk:
        kind = "мут" if r.kind == "mute" else "чат закрыт для записи"
        reason = escape((r.reason or "причина не найдена")[:220])
        link = f' <a href="{escape(r.reason_link)}">сообщение</a>' if r.reason_link else ""
        lines.append(
            f"{pe('user')} <b>{escape(r.account_label)}</b> · {escape(r.chat_title)}\n"
            f"   {kind}, до <b>{escape(fmt_until(r.until_at))}</b> "
            f"(с {fmt_local(r.detected_at)})\n"
            f"   {reason}{link}"
        )
    rows = []
    if total > 1:
        rows.append(nav_row("restr_mutes", page, total))
    rows.append([ib("Баны/муты", "restr", icon="shield")])
    rows.append(home_row())
    await safe_edit(query, "\n".join(lines)[:3900], _kb(rows))


@router.callback_query(MenuCB.filter(F.a == "restr_acc"))
async def cb_restr_acc(query: CallbackQuery) -> None:
    accounts = [a for a in await ctx.store.list_accounts() if a.telethon_session]
    restr = await ctx.store.list_restrictions()
    by_acc: dict[int, list] = {}
    for r in restr:
        by_acc.setdefault(r.account_id, []).append(r)
    rows = []
    for acc in accounts:
        items = by_acc.get(acc.id, [])
        bans = sum(1 for r in items if r.is_ban)
        mutes = len(items) - bans
        spam = "⛔" if acc.is_spam_limited else ""
        rows.append(
            [ib(f"{spam}{acc.label}: бан {bans} · мут {mutes}", "restr_a", acc.id, icon="user")]
        )
    rows.append([ib("Баны/муты", "restr", icon="shield")])
    rows.append(home_row())
    await safe_edit(
        query,
        prompt_html("Ограничения по аккаунтам", "Выберите аккаунт.", "user"),
        _kb(rows),
    )


async def account_restrictions_html(account_id: int) -> str:
    acc = await ctx.store.get_account(account_id)
    if not acc:
        return prompt_html("Ограничения", "Нет аккаунта.", "warn")
    items = await ctx.store.list_restrictions(account_id=account_id)
    lines = [f"{pe('user')} <b>{escape(acc.label)}</b>", ""]
    status = STATUS_LABEL.get(acc.spam_status, acc.spam_status)
    lines.append(
        f"{pe('shield')} @SpamBot: <b>{status}</b>"
        + (f" до {escape(acc.spam_until)}" if acc.spam_until else "")
        + (f" (проверен {fmt_local(acc.spam_checked_at)})" if acc.spam_checked_at else "")
    )
    bans = [r for r in items if r.is_ban]
    mutes = [r for r in items if r.is_mute]
    lines.append(f"\n{pe('block')} <b>Забанен в чатах</b> ({len(bans)}):")
    lines.extend(f"· {escape(r.chat_title)} (с {fmt_local(r.detected_at)})" for r in bans)
    if not bans:
        lines.append("<i>нет</i>")
    lines.append(f"\n{pe('clock')} <b>Мут / нельзя писать</b> ({len(mutes)}):")
    for r in mutes:
        reason = escape((r.reason or "причина не найдена")[:160])
        lines.append(f"· {escape(r.chat_title)} — до {escape(fmt_until(r.until_at))}: {reason}")
    if not mutes:
        lines.append("<i>нет</i>")
    return "\n".join(lines)[:3900]


@router.callback_query(MenuCB.filter(F.a == "restr_a"))
async def cb_restr_a(query: CallbackQuery, callback_data: MenuCB) -> None:
    text = await account_restrictions_html(callback_data.i)
    rows = [
        [ib("Проверить SpamBot", "spam_one", callback_data.i, icon="shield")],
        [ib("Аккаунт", "acc", callback_data.i, icon="user")],
        [ib("Баны/муты", "restr", icon="shield")],
        home_row(),
    ]
    await safe_edit(query, text, _kb(rows))


@router.callback_query(MenuCB.filter(F.a == "spam_one"))
async def cb_spam_one(query: CallbackQuery, callback_data: MenuCB) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc or not acc.telethon_session:
        await query.answer("Нужен Telethon session", show_alert=True)
        return
    await query.answer("Спрашиваю @SpamBot…")
    status = await check_account_spam(ctx.store, acc)
    text = await account_restrictions_html(acc.id)
    rows = [
        [ib("Проверить ещё раз", "spam_one", acc.id, icon="shield")],
        [ib("Аккаунт", "acc", acc.id, icon="user")],
        home_row(),
    ]
    await safe_edit(query, text, _kb(rows))
    if status == "limited":
        await query.message.answer("⚠ У аккаунта ограничение @SpamBot — смотрите «Советы».")


@router.callback_query(MenuCB.filter(F.a == "spam_all"))
async def cb_spam_all(query: CallbackQuery) -> None:
    if runtime.is_running("spam_check", 0):
        await query.answer("Проверка уже идёт", show_alert=True)
        return
    await query.answer("Запускаю")
    runtime.spawn(
        "spam_check",
        0,
        run_spam_check(ctx.store, bot=query.bot, admin_chat_id=query.from_user.id),
    )
    await query.message.answer("Проверяю все аккаунты через @SpamBot (по 3 параллельно) — пришлю итог.")


@router.callback_query(MenuCB.filter(F.a == "cov"))
async def cb_cov(query: CallbackQuery) -> None:
    await query.answer("Считаю…")
    text = await coverage_report(ctx.store)
    rows = [
        [ib("Вступить в недостающие (все акк.)", "join_miss_all", icon="users")],
        [ib("Обновить", "cov", icon="up"), ib("Баны/муты", "restr", icon="shield")],
        home_row(),
    ]
    from app.notify import split_text

    parts = split_text(text, 3500)
    await safe_edit(query, escape(parts[0]), _kb(rows))
    for extra in parts[1:]:
        await query.message.answer(escape(extra), parse_mode="HTML")


@router.callback_query(MenuCB.filter(F.a == "adv"))
async def cb_adv(query: CallbackQuery) -> None:
    await query.answer("Анализирую…")
    advice = await build_advice(ctx.store)
    icon = {"stop": "🛑", "warn": "⚠", "ok": "✓"}
    lines = [f"{pe('mega')} <b>Советы по аккаунтам</b>", ""]
    bad = [a for a in advice if a.level != "ok"]
    if not bad:
        lines.append("Проблемных аккаунтов нет.")
    for a in bad[:25]:
        lines.append(
            f"{icon[a.level]} <b>{escape(a.account.label)}</b>: работает в "
            f"{len(a.works_in)}/{a.total}, недоступно {len(a.blocked_in)}"
            + (" · SpamBot ограничен" if a.account.is_spam_limited else "")
        )
    lines.append(f"\nОК: {sum(1 for a in advice if a.level == 'ok')} из {len(advice)}")
    rows = [
        [ib("Отправить рекомендации (@arxixx)", "adv_send", icon="mega")],
        [ib("Обновить", "adv", icon="up"), ib("Баны/муты", "restr", icon="shield")],
        home_row(),
    ]
    await safe_edit(query, "\n".join(lines)[:3900], _kb(rows))


@router.callback_query(MenuCB.filter(F.a == "adv_send"))
async def cb_adv_send(query: CallbackQuery) -> None:
    await query.answer("Отправляю…")
    text = await run_advisor(ctx.store, query.bot, query.from_user.id, force=True)
    if text.startswith("Советы:"):
        await safe_send(query.bot, query.from_user.id, text)


@router.callback_query(MenuCB.filter(F.a == "join_miss_all"))
async def cb_join_miss_all(query: CallbackQuery) -> None:
    await safe_edit(
        query,
        prompt_html(
            "Вступить в недостающие",
            "Каждый Telethon-аккаунт проверит членство и вступит <b>только в те чаты каталога, "
            "где его нет</b> (invite / @username / гарант / капча).\n"
            f"{pe('block')} В чаты, где аккаунт забанен, и в выключенные вручную — не вступаем.\n"
            f"{pe('check')} После вступления пара сразу настраивается (без ожидания понедельника).",
            "users",
        ),
        confirm_kb(MenuCB(a="join_miss_all_go"), MenuCB(a="restr"), yes_text="Вступить"),
    )


@router.callback_query(MenuCB.filter(F.a == "join_miss_all_go"))
async def cb_join_miss_all_go(query: CallbackQuery) -> None:
    if runtime.is_running("join_missing_all", 0):
        await query.answer("Уже запущено", show_alert=True)
        return
    await query.answer("Запускаю")
    runtime.spawn(
        "join_missing_all",
        0,
        run_join_missing_all(ctx.store, query.bot, query.from_user.id),
    )
    await query.message.answer("Вступление в недостающие чаты запущено — итог придёт сюда.")
