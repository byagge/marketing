"""Чаты аккаунта: выключатели отправки, свой текст на чат, sender аккаунта, поиск."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.kb_pairs import bulk_confirm_kb, pair_card_kb, pair_list_kb
from app.bot.keyboards import MenuCB, cancel_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import PairSearch, PairText
from app.context import ctx
from app.jobs import runtime
from app.jobs.prefs import apply_account_sender, apply_pair
from app.ui.autopilot_screens import pair_card_html, pair_list_html
from app.ui.screens import prompt_html
from app.utils.chat_search import clamp_page, filter_chats
from app.utils.entities import from_aiogram_message

router = Router()

# (telegram user id, account id) -> строка поиска
_queries: dict[tuple[int, int], str] = {}


def _uid(event: CallbackQuery | Message) -> int:
    return event.from_user.id if event.from_user else 0


async def _list_payload(user_id: int, account_id: int, page: int):
    acc = await ctx.store.get_account(account_id)
    if not acc:
        return None, None
    query = _queries.get((user_id, account_id), "")
    all_chats = await ctx.store.list_chats()
    chats = filter_chats(all_chats, query)
    prefs = await ctx.store.prefs_for_account(account_id)
    bans = {b.chat_pk: b for b in await ctx.store.list_bans(account_id=account_id)}
    page = clamp_page(page, len(chats))
    text = pair_list_html(
        acc,
        shown=len(chats),
        total=len(all_chats),
        query=query,
        off_n=sum(1 for p in prefs.values() if not p.enabled),
        text_n=sum(1 for p in prefs.values() if p.has_text),
        ban_n=len(bans),
    )
    return text, pair_list_kb(account_id, chats, prefs, bans, page=page, query=query)


async def _card_payload(account_id: int, chat_pk: int):
    acc = await ctx.store.get_account(account_id)
    chat = await ctx.store.get_chat(chat_pk)
    if not acc or not chat:
        return None, None
    pref = await ctx.store.get_chat_pref(account_id, chat_pk)
    ban = await ctx.store.get_ban(account_id, chat_pk)
    join = await ctx.store.get_join_state(account_id, chat_pk)
    return pair_card_html(acc, chat, pref, ban, join), pair_card_kb(account_id, chat_pk, pref, ban)


def _spawn_pair_apply(account_id: int, chat_pk: int, bot, admin_chat_id: int) -> bool:
    async def _job():
        acc = await ctx.store.get_account(account_id)
        chat = await ctx.store.get_chat(chat_pk)
        result = await apply_pair(ctx.store, account_id, chat_pk, bot, admin_chat_id)
        if bot and acc and chat:
            try:
                await bot.send_message(
                    admin_chat_id, f"{acc.label} → «{chat.display_name}»: {result}"
                )
            except Exception:
                pass

    try:
        runtime.spawn("pair_apply", account_id, _job())
        return True
    except RuntimeError:
        return False


@router.callback_query(MenuCB.filter(F.a == "acp_list"))
async def cb_list(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _list_payload(_uid(query), callback_data.i, callback_data.p)
    if markup is None:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acp_search"))
async def cb_search(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(PairSearch.query)
    await state.update_data(account_id=callback_data.i)
    text = prompt_html(
        "Поиск чата",
        "Пришлите часть названия, @username или тега. Можно несколько слов.\n"
        "<code>-</code> — сбросить поиск.",
        "search",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(PairSearch.query, F.text)
async def on_search(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    account_id = int(data.get("account_id") or 0)
    value = (message.text or "").strip()
    key = (_uid(message), account_id)
    if value in {"-", "0"}:
        _queries.pop(key, None)
    else:
        _queries[key] = value
    text, markup = await _list_payload(_uid(message), account_id, 0)
    if markup is None:
        await message.answer("Аккаунт не найден")
        return
    await finish_input(message, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acp_clear"))
async def cb_clear(query: CallbackQuery, callback_data: MenuCB) -> None:
    _queries.pop((_uid(query), callback_data.i), None)
    text, markup = await _list_payload(_uid(query), callback_data.i, 0)
    if markup is None:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acp_open"))
async def cb_open(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _card_payload(callback_data.i, callback_data.p)
    if markup is None:
        await query.answer("Нет аккаунта или чата", show_alert=True)
        return
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acp_tgl"))
async def cb_toggle(query: CallbackQuery, callback_data: MenuCB) -> None:
    pref = await ctx.store.get_chat_pref(callback_data.i, callback_data.p)
    new_val = not pref.enabled
    await ctx.store.set_chat_send_enabled(callback_data.i, callback_data.p, new_val)
    started = _spawn_pair_apply(callback_data.i, callback_data.p, query.bot, query.from_user.id)
    note = "Отправка включена" if new_val else "Отправка выключена"
    await query.answer(note + ("" if started else " (применится следующим проходом)"))
    text, markup = await _card_payload(callback_data.i, callback_data.p)
    if markup is not None:
        await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acp_txt"))
async def cb_text(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    chat = await ctx.store.get_chat(callback_data.p)
    if not acc or not chat:
        await query.answer("Нет аккаунта или чата", show_alert=True)
        return
    await state.set_state(PairText.text)
    await state.update_data(account_id=acc.id, chat_pk=chat.id)
    text = prompt_html(
        "Свой текст",
        f"Аккаунт <b>{escape(acc.label)}</b> → чат <b>{escape(chat.display_name)}</b>\n"
        "Пришлите текст одним сообщением — он уйдёт <b>строго как есть</b> "
        "(с premium emoji и форматированием; <code>{{GARANT}}</code> подставится). "
        "Фото аккаунта для этого чата не прикладывается.\n"
        "<code>-</code> — убрать свой текст.",
        "bookmark",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(PairText.text, F.text)
async def on_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    account_id = int(data.get("account_id") or 0)
    chat_pk = int(data.get("chat_pk") or 0)
    raw = (message.text or "").strip()
    if raw in {"-", "0"}:
        await ctx.store.clear_chat_text(account_id, chat_pk)
        note = "Свой текст убран — берётся пост аккаунта."
    else:
        text, entities = from_aiogram_message(message)
        await ctx.store.set_chat_text(account_id, chat_pk, text, entities)
        note = f"Свой текст сохранён ({len(text)} симв.)."
    started = _spawn_pair_apply(account_id, chat_pk, message.bot, message.chat.id)
    note += " Применяю…" if started else " Применится следующей настройкой."
    card, markup = await _card_payload(account_id, chat_pk)
    if markup is None:
        await message.answer("Аккаунт или чат не найден")
        return
    await finish_input(message, f"{escape(note)}\n\n{card}", markup)


@router.callback_query(MenuCB.filter(F.a == "acp_txtclr"))
async def cb_text_clear(query: CallbackQuery, callback_data: MenuCB) -> None:
    await ctx.store.clear_chat_text(callback_data.i, callback_data.p)
    started = _spawn_pair_apply(callback_data.i, callback_data.p, query.bot, query.from_user.id)
    await query.answer("Свой текст убран" + ("" if started else " (применится позже)"))
    text, markup = await _card_payload(callback_data.i, callback_data.p)
    if markup is not None:
        await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acp_unban"))
async def cb_unban(query: CallbackQuery, callback_data: MenuCB) -> None:
    await ctx.store.clear_ban(callback_data.i, callback_data.p)
    await ctx.store.reset_pair_join(callback_data.i, callback_data.p)
    await query.answer("Метка бана снята — автопилот снова будет пробовать")
    text, markup = await _card_payload(callback_data.i, callback_data.p)
    if markup is not None:
        await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acp_bulk"))
async def cb_bulk(query: CallbackQuery, callback_data: MenuCB) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    q = _queries.get((_uid(query), acc.id), "")
    chats = filter_chats(await ctx.store.list_chats(), q)
    enable = int(callback_data.p)
    what = "Включить" if enable else "Выключить"
    scope = f"найденные по «{escape(q)}»" if q else "ВСЕ"
    await safe_edit(
        query,
        prompt_html(
            f"{what} отправку",
            f"Аккаунт <b>{escape(acc.label)}</b>: {what.lower()} отправку в {scope} чаты — "
            f"<b>{len(chats)}</b> шт.?",
            "warn",
        ),
        bulk_confirm_kb(acc.id, enable),
    )


@router.callback_query(MenuCB.filter(F.a == "acp_bulk_go"))
async def cb_bulk_go(query: CallbackQuery, callback_data: MenuCB) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    q = _queries.get((_uid(query), acc.id), "")
    chats = filter_chats(await ctx.store.list_chats(), q)
    enable = bool(callback_data.p)
    await ctx.store.set_chat_send_enabled_bulk(acc.id, [c.id for c in chats], enable)
    await query.answer(f"Готово: {len(chats)} чатов")

    bot = query.bot
    admin = query.from_user.id
    pks = [c.id for c in chats]

    async def _job():
        done = 0
        for pk in pks:
            await apply_pair(ctx.store, acc.id, pk, None, None)
            done += 1
        try:
            await bot.send_message(
                admin,
                f"{acc.label}: отправка {'включена' if enable else 'выключена'} "
                f"в {done} чатах, применено.",
            )
        except Exception:
            pass

    try:
        runtime.spawn("pair_apply_bulk", acc.id, _job())
    except RuntimeError:
        await query.message.answer("Предыдущее массовое применение ещё идёт — состояние сохранено.")
    text, markup = await _list_payload(_uid(query), acc.id, 0)
    if markup is not None:
        await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acc_sender"))
async def cb_sender_toggle(query: CallbackQuery, callback_data: MenuCB) -> None:
    from app.bot.handlers.accounts import show_account_card

    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    new_val = 0 if acc.sender_on else 1
    await ctx.store.update_account(acc.id, sender_enabled=new_val)
    bot = query.bot
    admin = query.from_user.id

    async def _job():
        result = await apply_account_sender(ctx.store, acc.id, bot, admin)
        try:
            await bot.send_message(admin, f"{acc.label}: {result}")
        except Exception:
            pass

    try:
        runtime.spawn("sender_toggle", acc.id, _job())
        tail = ""
    except RuntimeError:
        tail = " (применение уже идёт)"
    await query.answer(("Sender включён" if new_val else "Sender выключен") + tail)
    acc = await ctx.store.get_account(acc.id)
    if acc:
        await show_account_card(query, acc)
