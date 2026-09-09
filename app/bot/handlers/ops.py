from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.keyboards import (
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
from app.ui.screens import (
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
    jobs = await ctx.store.recent_jobs(15)
    from app.bot.keyboards import main_menu

    await safe_edit(query, reports_html(jobs), main_menu())


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
