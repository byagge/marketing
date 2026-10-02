"""Факты отправки: таблица за последний час, любой час и любые сутки."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.bot.keyboards import MenuCB, home_row, ib
from app.bot.render import safe_edit
from app.config import get_settings
from app.context import ctx
from app.jobs.facts import spawn_facts
from app.ui.facts_view import day_view, days_summary, hour_view, last_hour_view
from app.ui.screens import prompt_html
from app.utils.facts import day_start

router = Router()


def _hour_ref(days_ago: int, hour: int) -> datetime:
    tz = get_settings().tz
    base = day_start(datetime.now(timezone.utc), tz, days_ago)
    return base.replace(hour=hour)


def _to_ref(moment: datetime) -> tuple[int, int]:
    tz = get_settings().tz
    now = datetime.now(timezone.utc).astimezone(tz)
    days = (now.date() - moment.astimezone(tz).date()).days
    return days, moment.astimezone(tz).hour


def _last_hour_kb() -> InlineKeyboardMarkup:
    tz = get_settings().tz
    now = datetime.now(timezone.utc).astimezone(tz)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ib("Обновить", "fact", icon="up"),
                ib("Собрать сейчас", "fact_now", icon="search"),
            ],
            [
                ib("Пред. час", "fact_h", 0, now.hour - 1 if now.hour else 23, icon="down")
                if now.hour
                else ib("Пред. час", "fact_h", 1, 23, icon="down"),
                ib("Сутки", "fact_d", 0, icon="clock"),
            ],
            [ib("Другие дни", "fact_days", icon="stack")],
            [
                ib("Собрать за 24ч", "fact_back", 0, 24, icon="inbox"),
                ib("за 72ч", "fact_back", 0, 72, icon="inbox"),
            ],
            home_row(),
        ]
    )


@router.callback_query(MenuCB.filter(F.a == "fact"))
async def cb_fact(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Строю таблицу…")
    text, png = await last_hour_view(ctx.store)
    await safe_edit(query, text, _last_hour_kb(), photo=png)


@router.callback_query(MenuCB.filter(F.a == "fact_now"))
async def cb_fact_now(query: CallbackQuery) -> None:
    task = spawn_facts(
        ctx.store,
        hours=3,
        scan_restrictions=True,
        bot=query.bot,
        admin_chat_id=query.from_user.id,
    )
    if task is None:
        await query.answer("Сбор уже идёт", show_alert=True)
        return
    await query.answer("Собираю…")
    await query.message.answer(
        "Собираю историю чатов за 3 часа + проверяю баны/муты. "
        "Когда закончу — пришлю итог, потом нажмите «Обновить»."
    )


@router.callback_query(MenuCB.filter(F.a == "fact_back"))
async def cb_fact_back(query: CallbackQuery, callback_data: MenuCB) -> None:
    hours = max(1, min(int(callback_data.p or 24), 168))
    task = spawn_facts(
        ctx.store,
        hours=hours,
        scan_restrictions=False,
        bot=query.bot,
        admin_chat_id=query.from_user.id,
    )
    if task is None:
        await query.answer("Сбор уже идёт", show_alert=True)
        return
    await query.answer("Собираю…")
    await query.message.answer(
        f"Читаю историю чатов за {hours} ч (то, что реально отправлено, "
        f"до момента запуска бота данных не было). Пришлю итог."
    )


@router.callback_query(MenuCB.filter(F.a == "fact_days"))
async def cb_fact_days(query: CallbackQuery) -> None:
    rows = await days_summary(ctx.store, 7)
    kb = [
        [ib(f"{label} — {total} отправок", "fact_d", d, icon="clock")]
        for d, label, total in rows
    ]
    kb.append([ib("Последний час", "fact", icon="chart")])
    kb.append(home_row())
    await safe_edit(
        query,
        prompt_html("Факты по дням", "Выберите сутки — увидите часы, затем любой час.", "chart"),
        InlineKeyboardMarkup(inline_keyboard=kb),
    )


@router.callback_query(MenuCB.filter(F.a == "fact_d"))
async def cb_fact_day(query: CallbackQuery, callback_data: MenuCB) -> None:
    days_ago = max(0, int(callback_data.i))
    await query.answer("Строю…")
    text, png, per_hour = await day_view(ctx.store, days_ago)
    now_h = datetime.now(timezone.utc).astimezone(get_settings().tz).hour
    rows: list[list] = []
    hours = [h for h in range(24) if days_ago > 0 or h <= now_h]
    for i in range(0, len(hours), 4):
        rows.append(
            [
                ib(f"{h:02d}ч·{per_hour[h]}", "fact_h", days_ago, h, icon="clock")
                for h in hours[i : i + 4]
            ]
        )
    nav = []
    if days_ago < 30:
        nav.append(ib("Пред. день", "fact_d", days_ago + 1, icon="down"))
    if days_ago > 0:
        nav.append(ib("След. день", "fact_d", days_ago - 1, icon="up"))
    if nav:
        rows.append(nav)
    rows.append([ib("Последний час", "fact", icon="chart"), ib("Дни", "fact_days", icon="stack")])
    rows.append(home_row())
    await safe_edit(query, text, InlineKeyboardMarkup(inline_keyboard=rows), photo=png)


@router.callback_query(MenuCB.filter(F.a == "fact_h"))
async def cb_fact_hour(query: CallbackQuery, callback_data: MenuCB) -> None:
    days_ago = max(0, int(callback_data.i))
    hour = max(0, min(23, int(callback_data.p)))
    await query.answer("Строю…")
    text, png = await hour_view(ctx.store, days_ago, hour)
    ref = _hour_ref(days_ago, hour)
    prev_d, prev_h = _to_ref(ref - timedelta(hours=1))
    nav = [ib("−1 час", "fact_h", prev_d, prev_h, icon="down")]
    nxt = ref + timedelta(hours=1)
    if nxt <= datetime.now(timezone.utc):
        nd, nh = _to_ref(nxt)
        nav.append(ib("+1 час", "fact_h", nd, nh, icon="up"))
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            nav,
            [ib("Сутки", "fact_d", days_ago, icon="clock"), ib("Последний час", "fact", icon="chart")],
            home_row(),
        ]
    )
    await safe_edit(query, text, kb, photo=png)
