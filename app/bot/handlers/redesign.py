"""Раздел «Аккаунты для переоформления»: список, отметка «переоформил», проверка сейчас."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.kb_pairs import redesign_kb
from app.bot.keyboards import MenuCB
from app.bot.render import safe_edit
from app.context import ctx
from app.jobs import runtime
from app.jobs.perf import (
    mark_account_redesigned,
    redesign_candidates,
    redesign_report_html,
    run_perf_scan,
    toggle_account_skip,
)

router = Router()


async def _payload():
    items = [
        (acc.id, acc.label, bool(perf.skip))
        for acc, perf in await redesign_candidates(ctx.store)
    ]
    return await redesign_report_html(ctx.store), redesign_kb(items)


@router.callback_query(MenuCB.filter(F.a == "rd"))
async def cb_redesign(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _payload()
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "rd_done"))
async def cb_redesign_done(query: CallbackQuery, callback_data: MenuCB) -> None:
    await mark_account_redesigned(ctx.store, callback_data.i)
    await query.answer("Отмечено: переоформлен. Оценю заново через неделю")
    text, markup = await _payload()
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "rd_skip"))
async def cb_redesign_skip(query: CallbackQuery, callback_data: MenuCB) -> None:
    perf = await toggle_account_skip(ctx.store, callback_data.i)
    await query.answer("Больше не оцениваю этот аккаунт" if perf.skip else "Снова оцениваю")
    text, markup = await _payload()
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "rd_now"))
async def cb_redesign_now(query: CallbackQuery) -> None:
    bot, admin = query.bot, query.from_user.id

    async def _job():
        result = await run_perf_scan(ctx.store, bot, admin)
        text, _ = await _payload()
        try:
            await bot.send_message(admin, result)
        except Exception:
            pass

    try:
        runtime.spawn("perf_scan", 0, _job())
    except RuntimeError:
        await query.answer("Проверка уже идёт", show_alert=True)
        return
    await query.answer("Измеряю все аккаунты — итог придёт сюда (может занять несколько минут)")
