from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import MenuCB, cancel_kb, online_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import EditSettings
from app.context import ctx
from app.jobs.online import run_online_ping
from app.ui.screens import online_html, prompt_html

router = Router()


async def _screen():
    cfg = await ctx.store.online_ping_settings()
    accounts = await ctx.store.list_accounts()
    return online_html(cfg, accounts), online_kb(cfg)


@router.callback_query(MenuCB.filter(F.a == "online"))
async def cb_online(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _screen()
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "on_toggle"))
async def cb_toggle_global(query: CallbackQuery) -> None:
    cfg = await ctx.store.online_ping_settings()
    await ctx.store.update_online_ping_settings(enabled=not cfg.enabled)
    # If turning on — allow next tick soon.
    if not cfg.enabled:
        await ctx.store.update_online_ping_settings(next_at="")
    text, markup = await _screen()
    await safe_edit(query, text, markup)
    await query.answer("Включено" if not cfg.enabled else "Выключено")


@router.callback_query(MenuCB.filter(F.a == "on_hours"))
async def cb_hours(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditSettings.online_hours)
    text = prompt_html(
        "Интервал",
        "Как часто пинговать Online (часы).\n"
        "Например <code>3.5</code> ≈ каждые 3–4 часа.",
        "clock",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(EditSettings.online_hours, F.text)
async def on_hours(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        hours = float(raw)
    except ValueError:
        await message.answer("Число, например 3.5")
        return
    if hours < 0.5 or hours > 48:
        await message.answer("Диапазон: 0.5 … 48 часов")
        return
    await state.clear()
    await ctx.store.update_online_ping_settings(hours=hours, next_at="")
    text, markup = await _screen()
    await finish_input(message, text, markup)


@router.callback_query(MenuCB.filter(F.a == "on_now"))
async def cb_now(query: CallbackQuery) -> None:
    await query.answer("Пингую…")
    summary = await run_online_ping(ctx.store, force=True)
    text, markup = await _screen()
    # Escape angle brackets in summary for HTML safety
    from html import escape

    await safe_edit(
        query,
        f"{text}\n\n<code>{escape(summary)}</code>"[:3900],
        markup,
    )
