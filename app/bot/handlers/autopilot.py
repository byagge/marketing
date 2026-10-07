"""Автопилот, база банов, диагностика чата, лимит постов на аккаунт."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.kb_pairs import autopilot_kb, bans_kb, diag_kb
from app.bot.keyboards import MenuCB, cancel_kb, chat_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import ChatLimit
from app.context import ctx
from app.jobs import runtime
from app.jobs.autopilot import autopilot_enabled, run_autopilot
from app.jobs.bans import ban_report_html
from app.jobs.diagnose import diagnose_chat, diagnosis_html, live_membership
from app.jobs.prefs import apply_chat_limit
from app.ui.autopilot_screens import autopilot_html
from app.ui.screens import chat_html, prompt_html

router = Router()


async def _ap_payload():
    enabled = await autopilot_enabled(ctx.store)
    last_at = await ctx.store.get_setting("autopilot_last_at", "")
    summary = await ctx.store.get_setting("autopilot_last_summary", "")
    bans = await ctx.store.list_bans(active_only=True)
    running = runtime.is_running("autopilot", 0)
    return autopilot_html(enabled, last_at, summary, len(bans), running), autopilot_kb(enabled, running)


@router.callback_query(MenuCB.filter(F.a == "ap"))
async def cb_ap(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _ap_payload()
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "ap_toggle"))
async def cb_ap_toggle(query: CallbackQuery) -> None:
    new_val = not await autopilot_enabled(ctx.store)
    await ctx.store.set_setting("autopilot_enabled", "1" if new_val else "0")
    await query.answer("Автопилот включён" if new_val else "Автопилот выключен")
    text, markup = await _ap_payload()
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "ap_now"))
async def cb_ap_now(query: CallbackQuery) -> None:
    if runtime.is_running("autopilot", 0):
        await query.answer("Проход уже идёт", show_alert=True)
        return
    bot = query.bot
    admin = query.from_user.id

    async def _job():
        summary = await run_autopilot(ctx.store, bot, admin, force=True)
        # run_autopilot пишет сводку сам, если было что сообщить
        if summary.startswith(("Автопилот:", "Автопилот выключен")):
            try:
                await bot.send_message(admin, summary)
            except Exception:
                pass

    try:
        runtime.spawn("autopilot", 0, _job())
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)
        return
    await query.answer("Проход запущен — сводка придёт сюда")
    text, markup = await _ap_payload()
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "ap_reset"))
async def cb_ap_reset(query: CallbackQuery) -> None:
    n = await ctx.store.reset_join_states()
    await query.answer(f"Сброшено пар: {n} — автопилот попробует снова")
    text, markup = await _ap_payload()
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "bans"))
async def cb_bans(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(query, await ban_report_html(ctx.store), bans_kb())


@router.callback_query(MenuCB.filter(F.a.in_({"chat_diag", "chat_diag_live"})))
async def cb_diag(query: CallbackQuery, callback_data: MenuCB) -> None:
    live = callback_data.a == "chat_diag_live"
    members = None
    if live:
        await query.answer("Проверяю членство по Telethon…")
        members = await live_membership(ctx.store, callback_data.i)
    diag = await diagnose_chat(ctx.store, callback_data.i, live_members=members)
    if diag is None:
        await query.answer("Нет чата", show_alert=True)
        return
    await safe_edit(query, diagnosis_html(diag), diag_kb(callback_data.i))


@router.callback_query(MenuCB.filter(F.a == "chat_lim"))
async def cb_limit(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    await state.set_state(ChatLimit.value)
    await state.update_data(chat_pk=chat.id)
    text = prompt_html(
        "Лимит постов",
        f"Чат <b>{escape(chat.display_name)}</b>: сколько постов на <b>один аккаунт в сутки</b> "
        f"разрешено максимум?\n"
        "Например <code>3</code> — интервал аккаунта в чате станет не меньше 8 ч.\n"
        "<code>0</code> — без лимита.",
        "shield",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(ChatLimit.value, F.text)
async def on_limit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) > 200:
        await message.answer("Нужно целое число 0–200")
        return
    await state.clear()
    chat_pk = int(data.get("chat_pk") or 0)
    chat = await ctx.store.update_chat(chat_pk, max_posts_per_account=int(raw))
    if not chat:
        await message.answer("Чат не найден")
        return
    bot = message.bot
    admin = message.chat.id

    async def _job():
        result = await apply_chat_limit(ctx.store, chat_pk, bot, admin)
        try:
            await bot.send_message(admin, result)
        except Exception:
            pass

    try:
        runtime.spawn("chat_limit", chat_pk, _job())
        note = "Лимит сохранён, применяю ко всем аккаунтам…\n\n"
    except RuntimeError:
        note = "Лимит сохранён (применение уже идёт).\n\n"
    await finish_input(message, note + chat_html(chat), chat_kb(chat))
