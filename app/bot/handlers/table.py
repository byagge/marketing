from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards import MenuCB, cancel_kb, confirm_kb, table_chats_kb, table_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import EditTable
from app.context import ctx
from app.excel_table import export_tables, parse_import
from app.jobs import runtime
from app.jobs.reconfigure import (
    rebalance_minute_table,
    run_fix_assigned_minutes,
    run_reconfigure_all,
)
from app.tg.resolve import refresh_chat_titles_quiet
from app.ui.screens import prompt_html, table_html, table_index_html
from app.ui.table_image import try_chat_png, try_overview_png
from app.utils.minutes import TableFullError, period_for_interval, suggest_minute

router = Router()


def _table_kb(chats):
    return table_chats_kb(
        chats,
        fix_running=runtime.is_running("fix_minutes", 0),
        reall_running=runtime.is_running("reconfigure_all", 0),
    )


async def _index_photo():
    await refresh_chat_titles_quiet(ctx.store)
    chats = [c for c in await ctx.store.list_chats(kind="schedule") if c.enabled]
    accounts = await ctx.store.list_accounts()
    slots = await ctx.store.all_slots()
    return chats, try_overview_png(chats, accounts, slots)


async def _chat_photo(chat):
    slots = await ctx.store.slots_for_chat(chat.id)
    accounts = await ctx.store.list_accounts()
    return slots, accounts, try_chat_png(chat, slots, accounts)


@router.callback_query(MenuCB.filter(F.a == "table"))
async def cb_table(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Названия из Telegram…")
    chats, photo = await _index_photo()
    await safe_edit(query, table_index_html(len(chats)), _table_kb(chats), photo=photo)


@router.callback_query(MenuCB.filter(F.a == "tbl"))
async def cb_tbl(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    slots, accounts, photo = await _chat_photo(chat)
    await safe_edit(query, table_html(chat, slots), table_kb(chat, slots, accounts), photo=photo)


@router.callback_query(MenuCB.filter(F.a == "tbl_add"))
async def cb_add(query: CallbackQuery, callback_data: MenuCB) -> None:
    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    account_id = callback_data.p
    occupied = await ctx.store.occupied_minutes(chat.id)
    all_slots = await ctx.store.all_slots()
    schedule_ids = {
        c.id for c in await ctx.store.list_chats(kind="schedule") if c.enabled
    }
    global_counts: dict[int, int] = {}
    for slot in all_slots:
        if slot.chat_pk in schedule_ids:
            global_counts[slot.start_minute] = global_counts.get(slot.start_minute, 0) + 1
    try:
        minute = suggest_minute(
            period_for_interval(chat.interval_minutes), occupied, global_counts
        )
        await ctx.store.set_slot(chat.id, account_id, minute)
    except TableFullError:
        await query.answer("Таблица заполнена", show_alert=True)
        return
    except Exception as e:
        await query.answer(str(e), show_alert=True)
        return
    slots, accounts, photo = await _chat_photo(chat)
    await safe_edit(query, table_html(chat, slots), table_kb(chat, slots, accounts), photo=photo)


@router.callback_query(MenuCB.filter(F.a == "tbl_ed"))
async def cb_ed(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(EditTable.minute)
    await state.update_data(chat_pk=callback_data.i, account_id=callback_data.p)
    await safe_edit(
        query,
        prompt_html("Минута", "Число <b>0–59</b> или <code>-</code> чтобы убрать аккаунт.", "clock"),
        cancel_kb(),
    )
    await ask_input(query, prompt_html("Минута", "Жду минуту", "clock"))


@router.message(EditTable.minute, F.text)
async def on_minute(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if "chat_pk" not in data or "account_id" not in data:
        await state.clear()
        await message.answer("Сессия ввода сброшена")
        return
    raw = (message.text or "").strip()
    chat = await ctx.store.get_chat(int(data["chat_pk"]))
    if not chat:
        await state.clear()
        await message.answer("Чат не найден")
        return
    account_id = int(data["account_id"])
    await state.clear()
    if raw in {"-", "удалить"}:
        await ctx.store.delete_slot(chat.id, account_id)
    elif raw.isdigit() and 0 <= int(raw) <= 59:
        try:
            await ctx.store.set_slot(chat.id, account_id, int(raw))
        except Exception as e:
            await message.answer(f"Не записалось (минута занята?): {e}")
            slots, accounts, photo = await _chat_photo(chat)
            await finish_input(message, table_html(chat, slots), table_kb(chat, slots, accounts), photo)
            return
    else:
        await message.answer("Нужно число 0–59")
        return
    slots, accounts, photo = await _chat_photo(chat)
    await finish_input(message, table_html(chat, slots), table_kb(chat, slots, accounts), photo)


@router.callback_query(MenuCB.filter(F.a == "tbl_dl"))
async def cb_dl(query: CallbackQuery) -> None:
    chats = await ctx.store.list_chats()
    accounts = await ctx.store.list_accounts()
    slots = await ctx.store.all_slots()
    payload = export_tables(chats, accounts, slots)
    await query.answer()
    await query.message.answer_document(
        BufferedInputFile(payload, filename="minutes.xlsx"),
        caption="Можно править start_minute и залить обратно.",
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_ul"))
async def cb_ul(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditTable.upload)
    await safe_edit(query, prompt_html("Excel", "Пришлите <b>.xlsx</b> того же формата.", "inbox"), cancel_kb())
    await ask_input(query, prompt_html("Excel", "Жду файл", "inbox"))


@router.message(EditTable.upload, F.document)
async def on_xlsx(message: Message, state: FSMContext) -> None:
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".xlsx"):
        await message.answer("Нужен .xlsx")
        return
    from io import BytesIO

    buf = BytesIO()
    await message.bot.download(doc, destination=buf)
    rows = parse_import(buf.getvalue())
    ok = 0
    errors = []
    for chat_id, account_id, minute in rows:
        chat = await ctx.store.get_chat_by_tg(str(chat_id)) if chat_id else None
        if not chat:
            errors.append(f"чат {chat_id or '?'} не найден")
            continue
        try:
            await ctx.store.set_slot(chat.id, account_id, minute)
            ok += 1
        except Exception as e:
            errors.append(f"{chat.title} acc {account_id}: {e}")
    await state.clear()
    text = f"Импорт: {ok} слотов."
    if errors:
        text += "\n" + "\n".join(errors[:20])
    chats, photo = await _index_photo()
    await finish_input(message, prompt_html("Импорт", escape(text), "inbox"), _table_kb(chats), photo)


@router.callback_query(MenuCB.filter(F.a == "tbl_rebal"))
async def cb_rebal(query: CallbackQuery) -> None:
    await safe_edit(
        query,
        prompt_html(
            "Пересобрать таблицу",
            "Минуты будут распределены <b>равномерно по часу</b> "
            "(0–59, равные промежутки, сдвиг фазы между чатами).\n"
            "Полная сетка: все аккаунты × все schedule-чаты. "
            "В Telegram расписание не трогаем.",
            "stack",
        ),
        confirm_kb(MenuCB(a="tbl_rebal_go"), MenuCB(a="table"), yes_text="Пересобрать"),
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_rebal_go"))
async def cb_rebal_go(query: CallbackQuery) -> None:
    await query.answer("Пересобираю…")
    stats = await rebalance_minute_table(ctx.store, only_assigned=False)
    chats, photo = await _index_photo()
    await safe_edit(
        query,
        prompt_html(
            "Таблица",
            f"Готово: <b>{stats['slots']}</b> слотов "
            f"({stats['accounts']} акк. × {stats['chats']} чатов).\n"
            f"Откройте таблицу снова, чтобы увидеть PNG.",
            "check",
        ),
        _table_kb(chats),
        photo=photo,
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_fix"))
async def cb_fix(query: CallbackQuery) -> None:
    if runtime.is_running("fix_minutes", 0):
        await safe_edit(
            query,
            prompt_html(
                "Выравнивание идёт",
                "Можно остановить — текущий аккаунт допишет чат и остановится.",
                "clock",
            ),
            confirm_kb(
                MenuCB(a="tbl_fix_stop"),
                MenuCB(a="table"),
                yes_text="Остановить",
            ),
        )
        return
    await safe_edit(
        query,
        prompt_html(
            "Выровнять минуты",
            "1) Уже назначенные слоты — равные промежутки по часу\n"
            "2) Переназначить schedule в Telegram\n"
            "   параллельно ×8 аккаунтов (анти-Flood)\n\n"
            "Новые пары аккаунт×чат не создаём.",
            "clock",
        ),
        confirm_kb(MenuCB(a="tbl_fix_go"), MenuCB(a="table"), yes_text="Выровнять"),
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_fix_go"))
async def cb_fix_go(query: CallbackQuery) -> None:
    if runtime.is_running("fix_minutes", 0):
        await query.answer("Уже запущено", show_alert=True)
        return
    if runtime.is_running("reconfigure_all", 0):
        await query.answer("Сначала дождитесь «Перенастроить все»", show_alert=True)
        return
    await query.answer("Запускаю")

    async def _job():
        await run_fix_assigned_minutes(
            ctx.store,
            query.bot,
            query.from_user.id,
        )

    try:
        runtime.spawn("fix_minutes", 0, _job())
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)
        return
    chats, photo = await _index_photo()
    await safe_edit(
        query,
        prompt_html(
            "Выравнивание",
            "Запущено в фоне. В логах будет имя аккаунта у каждого чата.\n"
            "Кнопка «Остановить выравнивание» — на экране таблицы.",
            "clock",
        ),
        _table_kb(chats),
        photo=photo,
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_fix_stop"))
async def cb_fix_stop(query: CallbackQuery) -> None:
    if not runtime.is_running("fix_minutes", 0):
        await query.answer("Уже не запущено")
        chats, photo = await _index_photo()
        await safe_edit(
            query,
            table_index_html(len(chats)),
            _table_kb(chats),
            photo=photo,
        )
        return
    runtime.request_cancel("fix_minutes", 0)
    await query.answer("Останавливаю…")
    chats, photo = await _index_photo()
    await safe_edit(
        query,
        prompt_html(
            "Остановка",
            "Запросил остановку выравнивания минут. "
            "Текущая пачка допишет и выйдет.",
            "down",
        ),
        _table_kb(chats),
        photo=photo,
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_reall"))
async def cb_reall(query: CallbackQuery) -> None:
    if runtime.is_running("reconfigure_all", 0):
        await safe_edit(
            query,
            prompt_html(
                "Перенастройка идёт",
                "Можно остановить набор новых аккаунтов "
                "(уже запущенные setup доработают).",
                "robot",
            ),
            confirm_kb(
                MenuCB(a="tbl_reall_stop"),
                MenuCB(a="table"),
                yes_text="Остановить",
            ),
        )
        return
    await safe_edit(
        query,
        prompt_html(
            "Перенастроить все",
            "1) Полная минутная таблица равномерно по часу\n"
            "2) Пачками параллельно (×8): schedule + sender + клоакинг\n\n"
            "Займёт время — логи придут в чат.",
            "robot",
        ),
        confirm_kb(MenuCB(a="tbl_reall_go"), MenuCB(a="table"), yes_text="Запустить"),
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_reall_go"))
async def cb_reall_go(query: CallbackQuery) -> None:
    if runtime.is_running("reconfigure_all", 0):
        await query.answer("Уже запущено", show_alert=True)
        return
    if runtime.is_running("fix_minutes", 0):
        await query.answer("Сначала остановите выравнивание минут", show_alert=True)
        return
    await query.answer("Запускаю")

    async def _job():
        await run_reconfigure_all(
            ctx.store,
            query.bot,
            query.from_user.id,
            rebalance=True,
            setup_accounts=True,
        )

    try:
        runtime.spawn("reconfigure_all", 0, _job())
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)
        return
    chats, photo = await _index_photo()
    await safe_edit(
        query,
        prompt_html(
            "Перенастройка",
            "Запущено в фоне: сначала таблица, затем аккаунты пачками.\n"
            "Кнопка «Остановить перенастройку» — на экране таблицы.",
            "robot",
        ),
        _table_kb(chats),
        photo=photo,
    )


@router.callback_query(MenuCB.filter(F.a == "tbl_reall_stop"))
async def cb_reall_stop(query: CallbackQuery) -> None:
    if not runtime.is_running("reconfigure_all", 0):
        await query.answer("Уже не запущено")
        chats, photo = await _index_photo()
        await safe_edit(
            query,
            table_index_html(len(chats)),
            _table_kb(chats),
            photo=photo,
        )
        return
    runtime.request_cancel("reconfigure_all", 0)
    await query.answer("Останавливаю…")
    chats, photo = await _index_photo()
    await safe_edit(
        query,
        prompt_html(
            "Остановка",
            "Запросил остановку «Перенастроить все». "
            "Новые аккаунты не стартуют; уже идущие setup можно стопнуть на карточке.",
            "down",
        ),
        _table_kb(chats),
        photo=photo,
    )
