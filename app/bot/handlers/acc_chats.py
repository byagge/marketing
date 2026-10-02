"""Чаты аккаунта: вкл/выкл отправку, отметки, отдельный текст под чат."""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards import MenuCB, cancel_kb, home_row, ib
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import EditChatText
from app.config import POSTS_DIR
from app.context import ctx
from app.jobs.chatprefs import (
    ChatItem,
    apply_custom_text,
    cycle_mention,
    find_item,
    list_account_items,
    set_enabled,
)
from app.ui.emoji import icon_id, pe
from app.ui.screens import prompt_html
from app.utils.entities import from_aiogram_message
from app.utils.timefmt import fmt_until

router = Router()

PER_PAGE = 6


class AccChatCB(CallbackData, prefix="ac"):
    a: str  # t toggle | m mention | x text | xr reset text | l list
    acc: int
    k: str = ""
    p: int = 0


def _btn(text: str, a: str, acc: int, k: str, p: int, icon: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=AccChatCB(a=a, acc=acc, k=k, p=p).pack(),
        icon_custom_emoji_id=icon_id(icon),
    )


def _status(item: ChatItem) -> str:
    bits: list[str] = []
    if item.globally_off:
        bits.append("чат выкл в каталоге")
    r = item.restriction
    if r is not None:
        bits.append("БАН" if r.is_ban else f"мут до {fmt_until(r.until_at)}")
    elif item.state is not None and item.chat is not None and item.chat.is_schedule:
        if item.state.is_abandoned:
            bits.append(f"стоп {item.state.fail_count}/3")
        elif not item.state.is_ok:
            bits.append("не настроен")
    if item.has_text:
        bits.append("свой текст")
    return " · ".join(bits)


async def _screen(acc_id: int, page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    acc = await ctx.store.get_account(acc_id)
    if not acc:
        return None
    items = await list_account_items(ctx.store, acc)
    total = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total - 1))
    chunk = items[page * PER_PAGE : page * PER_PAGE + PER_PAGE]

    off = sum(1 for i in items if not i.enabled)
    lines = [
        f"{pe('users')} <b>Чаты аккаунта {escape(acc.label)}</b>",
        f"Всего: {len(items)} · выключено вручную: {off} · стр. {page + 1}/{total}",
        "✅ отправляет · ⛔ выключено · 👁 отметки · ✏ свой текст",
        "",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for n, item in enumerate(chunk, start=page * PER_PAGE + 1):
        mark = "✅" if item.enabled else "⛔"
        title = escape(item.title)
        link = item.link
        name = f'<a href="{escape(link)}">{title}</a>' if link else title
        kind = {"schedule": "schedule", "sender": "sender", "live": "Autoposter"}[item.kind]
        status = _status(item)
        lines.append(
            f"{n}. {mark} {name}\n"
            f"   <code>{escape(item.chat_id)}</code> · {kind}"
            + (f" · {escape(status)}" if status else "")
            + (f" · отметки: {item.mention_label}" if item.kind != "schedule" else "")
        )
        key = item.key
        rows.append(
            [
                _btn(f"{mark} {n}", "t", acc_id, key, page, "check" if item.enabled else "block"),
                _btn(
                    f"👁 {n}: {item.mention_label}" if item.kind != "schedule" else f"👁 {n}: —",
                    "m",
                    acc_id,
                    key,
                    page,
                    "users",
                ),
                _btn(f"✏ {n}", "x", acc_id, key, page, "mega"),
            ]
        )
    if not items:
        lines.append("<i>Нет чатов.</i>")
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("Назад", "l", acc_id, "", page - 1, "down"))
    if page + 1 < total:
        nav.append(_btn("Вперёд", "l", acc_id, "", page + 1, "up"))
    if nav:
        rows.append(nav)
    rows.append([ib("Аккаунт", "acc", acc_id, icon="user")])
    rows.append(home_row())
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(inline_keyboard=rows)


async def show(query: CallbackQuery | Message, acc_id: int, page: int = 0) -> None:
    built = await _screen(acc_id, page)
    if built is None:
        if isinstance(query, CallbackQuery):
            await query.answer("Нет аккаунта", show_alert=True)
        return
    text, markup = built
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acc_cl"))
async def cb_open(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Загружаю чаты…")
    await show(query, callback_data.i, callback_data.p)


@router.callback_query(AccChatCB.filter(F.a == "l"))
async def cb_list(query: CallbackQuery, callback_data: AccChatCB) -> None:
    await show(query, callback_data.acc, callback_data.p)


@router.callback_query(AccChatCB.filter(F.a == "t"))
async def cb_toggle(query: CallbackQuery, callback_data: AccChatCB) -> None:
    acc = await ctx.store.get_account(callback_data.acc)
    item = await find_item(ctx.store, acc, callback_data.k) if acc else None
    if not acc or not item:
        await query.answer("Чат не найден", show_alert=True)
        return
    new_state = not item.enabled
    await query.answer("Применяю…")
    note = await set_enabled(ctx.store, acc, item, new_state)
    await show(query, acc.id, callback_data.p)
    head = "Включено" if new_state else "Выключено"
    await query.message.answer(f"{head}: {item.title}" + (f"\n{note}" if note else ""))
    if new_state and item.chat is not None and item.chat.is_schedule:
        asyncio.create_task(_schedule_now(acc.id, item, query.bot, query.from_user.id))


async def _schedule_now(account_id: int, item: ChatItem, bot, admin_chat_id: int) -> None:
    from app.jobs.setup import run_setup_chats_only

    if item.chat is None:
        return
    try:
        await run_setup_chats_only(
            ctx.store,
            account_id,
            [item.chat.id],
            bot,
            admin_chat_id,
            job_kind="setup_retry",
            skip_abandoned=False,
        )
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(AccChatCB.filter(F.a == "m"))
async def cb_mention(query: CallbackQuery, callback_data: AccChatCB) -> None:
    acc = await ctx.store.get_account(callback_data.acc)
    item = await find_item(ctx.store, acc, callback_data.k) if acc else None
    if not acc or not item:
        await query.answer("Чат не найден", show_alert=True)
        return
    note = await cycle_mention(ctx.store, acc, item)
    await query.answer(note[:180], show_alert=item.kind == "schedule")
    if item.kind != "schedule":
        await show(query, acc.id, callback_data.p)


@router.callback_query(AccChatCB.filter(F.a == "x"))
async def cb_text(query: CallbackQuery, callback_data: AccChatCB, state: FSMContext) -> None:
    acc = await ctx.store.get_account(callback_data.acc)
    item = await find_item(ctx.store, acc, callback_data.k) if acc else None
    if not acc or not item:
        await query.answer("Чат не найден", show_alert=True)
        return
    await state.set_state(EditChatText.text)
    await state.update_data(acc=acc.id, key=item.key, page=callback_data.p)
    cur = await ctx.store.get_chat_post(acc.id, item.chat_id)
    preview = (
        f"\n\nСейчас:\n<code>{escape((cur.text or '')[:600])}</code>" if cur else
        "\n\nСейчас используется общий пост аккаунта."
    )
    text = prompt_html(
        f"Свой текст · {escape(item.title)}",
        f"Аккаунт: <b>{escape(acc.label)}</b>\n"
        "Пришлите текст (можно с фото и premium emoji) — он будет уходить в этот чат "
        "вместо общего поста. Можно <code>{{GARANT}}</code>.\n"
        "Чтобы вернуть общий пост — отправьте <code>сброс</code>."
        + preview,
        "mega",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, prompt_html("Текст чата", "Жду текст или «сброс»", "mega"))


@router.message(EditChatText.text, F.text | F.photo | F.caption)
async def on_chat_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    acc_id = int(data.get("acc") or 0)
    key = str(data.get("key") or "")
    page = int(data.get("page") or 0)
    acc = await ctx.store.get_account(acc_id)
    item = await find_item(ctx.store, acc, key) if acc else None
    if not acc or not item:
        await state.clear()
        await message.answer("Чат не найден")
        return
    await state.clear()
    raw = (message.text or "").strip().casefold()
    if raw in {"сброс", "reset", "-"}:
        await ctx.store.delete_chat_post(acc.id, item.chat_id)
        note = await apply_custom_text(ctx.store, acc, item, message.bot, message.chat.id)
        built = await _screen(acc.id, page)
        if built:
            await finish_input(message, built[0], built[1])
        await message.answer(f"Общий пост восстановлен: {note}")
        return
    text, entities = from_aiogram_message(message)
    photo_path = ""
    dest_dir = POSTS_DIR / str(acc.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if message.photo:
        dest = dest_dir / f"chat_{item.key}.jpg"
        await message.bot.download(message.photo[-1], destination=dest)
        photo_path = str(dest)
    await ctx.store.save_chat_post(acc.id, item.chat_id, text, entities, photo_path)
    note = await apply_custom_text(ctx.store, acc, item, message.bot, message.chat.id)
    built = await _screen(acc.id, page)
    if built:
        await finish_input(message, built[0], built[1])
    await message.answer(f"Текст для «{item.title}» сохранён: {note}")
