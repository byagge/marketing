from __future__ import annotations

import logging

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.keyboards import (
    reports_kb,
    BTN_CANCEL,
    MenuCB,
    confirm_kb,
    home_row,
    ib,
    setup_kb,
)
from app.bot.handlers.accounts import refresh_account_card, show_account_card
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import LeaveFSM
from app.context import ctx
from app.jobs import runtime
from app.jobs.health import run_health, run_health_all
from app.jobs.leave import preview_leave, run_leave
from app.jobs.setup import run_setup
from app.reporting.dashboard import build_dashboard, format_dashboard_html
from app.ui.screens import (
    incidents_html,
    health_html,
    prompt_html,
    reports_html,
    setup_ask_html,
    setup_html,
)

router = Router()


def _running_ids() -> set[int]:
    ids = set()
    for key in runtime.running_keys():
        if key.startswith("setup:"):
            ids.add(int(key.split(":")[1]))
    return ids


@router.callback_query(MenuCB.filter(F.a == "setup"))
async def cb_setup(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    accounts = await ctx.store.list_accounts()
    await safe_edit(query, setup_html(), setup_kb(accounts, _running_ids()))


@router.callback_query(MenuCB.filter(F.a == "setup_ask"))
async def cb_setup_ask(query: CallbackQuery, callback_data: MenuCB) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    chats = await ctx.store.list_chats(enabled_only=True)
    sch = [c for c in chats if c.is_schedule]
    snd = [c for c in chats if c.kind == "sender"]
    ru = await ctx.store.get_post(acc.id, "ru")
    en = await ctx.store.get_post(acc.id, "en")
    text = setup_ask_html(
        acc,
        len(sch),
        len(snd),
        bool((ru.text or "").strip()),
        bool((en.text or "").strip()),
    )
    await safe_edit(
        query,
        text,
        confirm_kb(
            MenuCB(a="setup_go", i=acc.id),
            MenuCB(a="setup"),
            yes_text="Запустить",
        ),
    )


@router.callback_query(MenuCB.filter(F.a == "setup_go"))
async def cb_setup_go(query: CallbackQuery, callback_data: MenuCB) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    if runtime.is_running("setup", acc.id):
        await query.answer("Уже запущено", show_alert=True)
        return
    await query.answer("Запускаю")
    chat_id = query.from_user.id
    try:
        runtime.spawn(
            "setup",
            acc.id,
            run_setup(ctx.store, acc.id, query.bot, chat_id),
            on_done=lambda aid=acc.id: refresh_account_card(aid),
        )
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)
        return
    await show_account_card(query, acc)


@router.callback_query(MenuCB.filter(F.a == "setup_stop"))
async def cb_setup_stop(query: CallbackQuery, callback_data: MenuCB) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    if not runtime.is_running("setup", acc.id):
        await query.answer("Уже не запущено")
        await show_account_card(query, acc)
        return
    runtime.request_cancel("setup", acc.id)
    await query.answer("Останавливаю…")
    await show_account_card(query, acc)


@router.callback_query(MenuCB.filter(F.a == "health"))
async def cb_health(query: CallbackQuery) -> None:
    accounts = await ctx.store.list_accounts()
    rows = [[ib("Все аккаунты", "health_all", icon="search")]]
    for acc in accounts:
        rows.append([ib(acc.label, "health_go", acc.id, icon="user")])
    rows.append(home_row())
    await safe_edit(query, health_html(), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(MenuCB.filter(F.a == "health_all"))
async def cb_health_all(query: CallbackQuery) -> None:
    await query.answer("Проверяю все…")
    await run_health_all(ctx.store, query.bot, query.from_user.id)


@router.callback_query(MenuCB.filter(F.a == "health_go"))
async def cb_health_go(query: CallbackQuery, callback_data: MenuCB) -> None:
    await query.answer("Проверяю…")
    await query.message.answer(f"Проверка аккаунта id={callback_data.i}…")
    await run_health(ctx.store, callback_data.i, query.bot, query.from_user.id)


@router.callback_query(MenuCB.filter(F.a == "reports"))
async def cb_reports(query: CallbackQuery) -> None:
    await query.answer()
    await _show_report(query, day_offset=0)


@router.callback_query(MenuCB.filter(F.a == "rep_day"))
async def cb_rep_day(query: CallbackQuery, callback_data: MenuCB) -> None:
    await query.answer()
    await _show_report(query, day_offset=int(callback_data.p or 0))


async def _show_report(query: CallbackQuery, *, day_offset: int = 0) -> None:
    from app.config import get_settings
    from app.reporting.dashboard import day_offsets
    from app.ui.screens import prompt_html

    settings = get_settings()
    days = day_offsets(settings.timezone, 7)
    day = days[min(day_offset, len(days) - 1)][1] if days else None
    try:
        dash = await build_dashboard(ctx.store, day=day)
        text = format_dashboard_html(dash)
        kb = reports_kb(
            marketer_on=dash.marketer_enabled,
            auto_fix=dash.auto_fix,
            day_offset=day_offset,
            days=days,
        )
        await safe_edit(query, text, kb)
    except Exception as e:
        import logging

        logging.getLogger("marketing.reports").exception("reports failed")
        await safe_edit(
            query,
            prompt_html(
                "Отчётность",
                f"Не удалось открыть отчёт: <code>{type(e).__name__}</code>. "
                f"Попробуй ещё раз.",
                "warn",
            ),
            reports_kb(days=days, day_offset=day_offset),
        )


@router.callback_query(MenuCB.filter(F.a == "rep_jobs"))
async def cb_rep_jobs(query: CallbackQuery) -> None:
    jobs = await ctx.store.recent_jobs(15)
    dash_on = (await ctx.store.get_setting("marketer_enabled", "1")) == "1"
    auto = (await ctx.store.get_setting("marketer_auto_fix", "1")) == "1"
    from app.config import get_settings
    from app.reporting.dashboard import day_offsets

    days = day_offsets(get_settings().timezone, 7)
    await safe_edit(
        query,
        reports_html(jobs),
        reports_kb(marketer_on=dash_on, auto_fix=auto, days=days),
    )


@router.callback_query(MenuCB.filter(F.a == "rep_issues"))
async def cb_rep_issues(query: CallbackQuery) -> None:
    incidents = await ctx.store.list_incidents(statuses=["open", "fixing"], limit=40)
    dash_on = (await ctx.store.get_setting("marketer_enabled", "1")) == "1"
    auto = (await ctx.store.get_setting("marketer_auto_fix", "1")) == "1"
    from app.config import get_settings
    from app.reporting.dashboard import day_offsets

    days = day_offsets(get_settings().timezone, 7)
    await safe_edit(
        query,
        incidents_html(incidents),
        reports_kb(marketer_on=dash_on, auto_fix=auto, days=days),
    )


@router.callback_query(MenuCB.filter(F.a == "rep_density"))
async def cb_rep_density(query: CallbackQuery, callback_data: MenuCB) -> None:
    from html import escape

    from app.config import get_settings
    from app.reporting.dashboard import day_offsets
    from app.ui.emoji import pe
    from app.ui.screens import prompt_html

    settings = get_settings()
    days = day_offsets(settings.timezone, 7)
    off = int(callback_data.p or 0)
    day = days[min(off, len(days) - 1)][1]
    rows = await ctx.store.density_for_day(day)
    lines = [f"Плотность за <code>{escape(day)}</code>\n"]
    if not rows:
        lines.append("Пока нет данных — подожди тихий сбор (каждые ~20 мин).")
    for d in rows:
        gap = float(d.get("avg_gap_min") or 0)
        st = d.get("status") or ""
        mark = {
            "ok": "✓",
            "sparse": "⚠ нужно больше акк.",
            "dense": "🔥 слишком часто",
            "empty": "✗ пусто",
        }.get(st, st)
        title = escape(str(d.get("chat_title") or d.get("chat_pk")))
        lines.append(
            f"{pe('pin')} <b>{title}</b>\n"
            f"   {gap:.1f} мин между постами · {d.get('accounts_live', 0)} акк. · {mark}\n"
            f"   <i>{escape((d.get('advice') or '')[:200])}</i>"
        )
    dash_on = (await ctx.store.get_setting("marketer_enabled", "1")) == "1"
    auto = (await ctx.store.get_setting("marketer_auto_fix", "1")) == "1"
    await safe_edit(
        query,
        prompt_html("Плотность / советы", "\n".join(lines)[:3500], "pin"),
        reports_kb(marketer_on=dash_on, auto_fix=auto, day_offset=off, days=days),
    )


@router.callback_query(MenuCB.filter(F.a == "rep_leads"))
async def cb_rep_leads(query: CallbackQuery, callback_data: MenuCB) -> None:
    from html import escape

    from app.config import get_settings
    from app.reporting.dashboard import day_offsets
    from app.ui.emoji import pe
    from app.ui.screens import prompt_html

    settings = get_settings()
    days = day_offsets(settings.timezone, 7)
    off = int(callback_data.p or 0)
    day = days[min(off, len(days) - 1)][1]
    rows = await ctx.store.lead_stats_for_day(day)
    total_new = sum(r.new_leads for r in rows)
    total_msg = sum(r.messages for r in rows)
    lines = [
        f"Лиды за <code>{escape(day)}</code>\n",
        f"{pe('users')} Новых людей: <b>{total_new}</b>",
        f"{pe('mega')} Сообщений в ЛС: <b>{total_msg}</b>\n",
    ]
    if not rows:
        lines.append("Пока пусто — сбор идёт в фоне.")
    for r in rows:
        lines.append(
            f"· <b>{escape(r.account_label or str(r.account_id))}</b> — "
            f"+{r.new_leads} · {r.messages} сообщ."
        )
    dash_on = (await ctx.store.get_setting("marketer_enabled", "1")) == "1"
    auto = (await ctx.store.get_setting("marketer_auto_fix", "1")) == "1"
    await safe_edit(
        query,
        prompt_html("Лиды", "\n".join(lines)[:3500], "users"),
        reports_kb(marketer_on=dash_on, auto_fix=auto, day_offset=off, days=days),
    )


@router.callback_query(MenuCB.filter(F.a == "mk_toggle"))
async def cb_mk_toggle(query: CallbackQuery) -> None:
    cur = (await ctx.store.get_setting("marketer_enabled", "1")) == "1"
    await ctx.store.set_setting("marketer_enabled", "0" if cur else "1")
    await query.answer("Мониторинг выкл" if cur else "Мониторинг 24/7 вкл")
    await _show_report(query, day_offset=0)


@router.callback_query(MenuCB.filter(F.a == "mk_autofix"))
async def cb_mk_autofix(query: CallbackQuery) -> None:
    cur = (await ctx.store.get_setting("marketer_auto_fix", "1")) == "1"
    await ctx.store.set_setting("marketer_auto_fix", "0" if cur else "1")
    await query.answer("Автофикс выкл" if cur else "Автофикс вкл")
    await _show_report(query, day_offset=0)


@router.callback_query(MenuCB.filter(F.a == "leave_go"))
async def cb_leave_go(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await query.answer("Считаю диалоги…")
    try:
        kept, to_leave = await preview_leave(ctx.store, callback_data.i)
    except Exception as e:
        await query.message.answer(f"Не удалось: {e}")
        return
    preview_keep = "\n".join(f"KEEP  {x}" for x in kept[:15]) or "—"
    preview_del = "\n".join(f"DEL   {x}" for x in to_leave[:15])
    extra = f"\n… ещё {len(to_leave) - 15}" if len(to_leave) > 15 else ""

    text = prompt_html(
        "Leave all except",
        f"Останутся ({len(kept)}):\n<code>{escape(preview_keep)}</code>\n\n"
        f"К удалению ({len(to_leave)}):\n<code>{escape(preview_del)}</code>{escape(extra)}\n\n"
        "Напишите <code>YES</code> чтобы подтвердить.",
        "block",
    )
    await state.set_state(LeaveFSM.confirm)
    await state.update_data(account_id=callback_data.i)
    back = InlineKeyboardMarkup(
        inline_keyboard=[
            [ib(BTN_CANCEL, "acc", callback_data.i, icon="block")],
            home_row(),
        ]
    )
    await query.message.answer(text, reply_markup=back, parse_mode="HTML")
    await ask_input(query, prompt_html("Подтверждение", "Напишите <code>YES</code>", "warn"))


@router.message(LeaveFSM.confirm, F.text)
async def on_leave_yes(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() != "YES":
        await message.answer("Нужно в точности YES, либо Отмена.")
        return
    data = await state.get_data()
    await state.clear()
    if "account_id" not in data:
        await message.answer("Сессия сброшена")
        return
    account_id = int(data["account_id"])
    await message.answer("Выхожу…")
    await run_leave(ctx.store, account_id, message.bot, message.chat.id)
    acc = await ctx.store.get_account(account_id)
    if acc:
        from app.bot.handlers.accounts import _account_payload

        text, markup = await _account_payload(acc)
        await finish_input(message, text, markup)
