"""Перераспределение слотов по факту, сбор фактов, экспорт отчёта."""

from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.keyboards import MenuCB, home_row, ib
from app.bot.render import safe_edit
from app.context import ctx
from app.jobs import maintenance_lock, runtime
from app.jobs.balance import run_smart_rebalance
from app.jobs.export import send_report
from app.jobs.facts import run_collect_facts
from app.ui.screens import prompt_html
from app.utils.errfmt import short_error

router = Router()
log = logging.getLogger("marketing.balance.ui")


def _table_back() -> list:
    return [ib("Таблицы", "table", icon="clock")]


@router.callback_query(MenuCB.filter(F.a == "tbl_smart"))
async def cb_smart(query: CallbackQuery) -> None:
    busy = runtime.is_running("smart_rebalance", 0) or maintenance_lock.locked()
    body = (
        "Убирает из сетки аккаунты, которые <b>реально не пишут</b> (не в чате, мут, "
        "молчат), и раздвигает остальных равномерно — чтобы не было пауз вроде 42 минут. "
        "Пересобирает расписание только у тех, у кого слот изменился.\n\n"
        "• «План» — только показать, что изменится\n"
        "• «Применить» — собрать факты (если старше 2 ч) и применить\n"
        "• «Собрать факты» — обновить данные без изменений\n\n"
        "Автоматически это уже идёт каждые несколько часов."
    )
    if busy:
        body = "⏳ <b>Сейчас идёт другая тяжёлая операция</b> — подождите.\n\n" + body
    rows = [
        [ib("План (без изменений)", "tbl_smart_plan", icon="search")],
        [ib("Применить", "tbl_smart_go", icon="check")],
        [ib("Собрать факты", "tbl_facts", icon="stack")],
        _table_back(),
        home_row(),
    ]
    await safe_edit(
        query,
        prompt_html("Перераспределить по факту", body, "clock"),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_smart_plan"))
async def cb_smart_plan(query: CallbackQuery) -> None:
    await query.answer("Считаю по последним фактам…")
    report = await run_smart_rebalance(
        ctx.store, None, None, collect=False, dry_run=True, notify="never"
    )
    await query.message.answer(escape(report)[:3900])


@router.callback_query(MenuCB.filter(F.a == "tbl_smart_go"))
async def cb_smart_go(query: CallbackQuery) -> None:
    if runtime.is_running("smart_rebalance", 0) or maintenance_lock.locked():
        await query.answer("Уже идёт другая операция", show_alert=True)
        return
    await query.answer("Запускаю в фоне")
    admin_id = query.from_user.id

    async def _job():
        await run_smart_rebalance(
            ctx.store, query.bot, admin_id, collect=True, notify="always"
        )

    try:
        runtime.spawn("smart_rebalance", 0, _job())
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)
        return
    await query.message.answer(
        "Перераспределение запущено в фоне. Итог пришлю сообщением "
        "(сбор фактов может занять несколько минут)."
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_facts"))
async def cb_facts(query: CallbackQuery) -> None:
    if runtime.is_running("collect_facts", 0):
        await query.answer("Уже собираю", show_alert=True)
        return
    await query.answer("Собираю факты в фоне")
    admin_id = query.from_user.id

    async def _job():
        try:
            stats = await run_collect_facts(ctx.store)
            text = (
                f"Факты собраны: аккаунтов {stats['ok']}/{stats['accounts']}, "
                f"пар {stats['pairs']}, ошибок {stats['failed']}"
            )
        except Exception as e:
            text = f"Сбор фактов не удался: {short_error(e)}"
        try:
            await query.bot.send_message(admin_id, text)
        except Exception:
            pass

    try:
        runtime.spawn("collect_facts", 0, _job())
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)


# ---------- экспорт ----------


@router.callback_query(MenuCB.filter(F.a == "export"))
async def cb_export(query: CallbackQuery) -> None:
    rows = [
        [ib("Быстро (из базы)", "export_go", icon="inbox")],
        [ib("Со свежим сбором (долго)", "export_fresh", icon="search")],
        home_row(),
    ]
    await safe_edit(
        query,
        prompt_html(
            "Экспорт отчёта",
            "ZIP со <b>всеми данными</b>: аккаунты, чаты, слоты, состояния настройки, "
            "факты (состоит/мут/отправки за 24 ч), задачи, логи, настройки, найденные "
            "проблемы. Сессии и токены не включаются, телефоны и invite-ссылки маскируются.\n\n"
            "• <b>Быстро</b> — мгновенно, по последним сохранённым фактам\n"
            "• <b>Со свежим сбором</b> — сначала обновит факты и проверит клоакинг "
            "в Autoposter (несколько минут)\n\n"
            "Пришлите файл мне — разберу ошибки. Текстовые отчёты маркетолога "
            "(ЛС, SpamBot) приложите рядом: в базе бота их нет.",
            "inbox",
        ),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _export_job(bot, chat_id: int, fresh: bool) -> None:
    try:
        await send_report(ctx.store, bot, chat_id, fresh=fresh)
    except Exception as e:
        log.exception("export failed")
        try:
            await bot.send_message(chat_id, f"Экспорт не удался: {short_error(e)}")
        except Exception:
            pass


@router.callback_query(MenuCB.filter(F.a == "export_go"))
async def cb_export_go(query: CallbackQuery) -> None:
    await query.answer("Собираю…")
    await _export_job(query.bot, query.from_user.id, fresh=False)


@router.callback_query(MenuCB.filter(F.a == "export_fresh"))
async def cb_export_fresh(query: CallbackQuery) -> None:
    if runtime.is_running("export", 0):
        await query.answer("Экспорт уже идёт", show_alert=True)
        return
    await query.answer("Запускаю в фоне")
    await query.message.answer("Собираю свежие факты, потом пришлю ZIP…")
    try:
        runtime.spawn("export", 0, _export_job(query.bot, query.from_user.id, True))
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    await message.answer("Собираю отчёт…")
    await _export_job(message.bot, message.chat.id, fresh=False)
