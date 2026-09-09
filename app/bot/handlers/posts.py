from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import MenuCB, cancel_kb, posts_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import EditPost
from app.config import POSTS_DIR
from app.context import ctx
from app.ui.emoji import pe
from app.ui.screens import posts_html, prompt_html
from app.utils.entities import entities_loads, from_aiogram_message
from app.utils.templates import render_post

router = Router()


async def _posts_screen(account_id: int) -> tuple[str, object]:
    acc = await ctx.store.get_account(account_id)
    if not acc:
        return prompt_html("Пост", "Аккаунт не найден.", "warn"), None
    ru = await ctx.store.get_post(acc.id, "ru")
    en = await ctx.store.get_post(acc.id, "en")
    return posts_html(acc, ru, en), posts_kb(acc.id)


@router.callback_query(MenuCB.filter(F.a == "posts"))
async def cb_posts(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    if not callback_data.i:
        await query.answer("Откройте Пост внутри аккаунта", show_alert=True)
        return
    text, markup = await _posts_screen(callback_data.i)
    if markup is None:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a.in_({"post_ru", "post_en"})))
async def cb_set_post(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    if not callback_data.i:
        await query.answer("Откройте Пост внутри аккаунта", show_alert=True)
        return
    lang = "ru" if callback_data.a == "post_ru" else "en"
    await state.set_state(EditPost.ru if lang == "ru" else EditPost.en)
    await state.update_data(lang=lang, account_id=callback_data.i)
    acc = await ctx.store.get_account(callback_data.i)
    label = escape(acc.label) if acc else f"#{callback_data.i}"
    text = prompt_html(
        f"Пост {lang.upper()}",
        f"Аккаунт: <b>{label}</b>\n"
        "Пришлите одним сообщением. Текст, premium emoji и фото сохранятся "
        "только у этого аккаунта.\n"
        "Можно <code>{{GARANT}}</code>.",
        "mega",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


async def _save_post_message(message: Message, account_id: int, lang: str) -> None:
    text, entities = from_aiogram_message(message)
    photo_path = ""
    dest_dir = POSTS_DIR / str(account_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if message.photo:
        dest = dest_dir / f"{lang}.jpg"
        await message.bot.download(message.photo[-1], destination=dest)
        photo_path = str(dest)
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        dest = dest_dir / f"{lang}_{message.document.file_name or 'file'}"
        await message.bot.download(message.document, destination=dest)
        photo_path = str(dest)
    await ctx.store.save_post(account_id, lang, text, entities, photo_path or None)
    screen, markup = await _posts_screen(account_id)
    await finish_input(message, screen, markup or cancel_kb())


@router.message(EditPost.ru, F.text | F.photo | F.caption)
async def on_ru(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    account_id = data.get("account_id")
    if not account_id:
        await message.answer("Откройте Пост внутри аккаунта")
        return
    await _save_post_message(message, int(account_id), "ru")


@router.message(EditPost.en, F.text | F.photo | F.caption)
async def on_en(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    account_id = data.get("account_id")
    if not account_id:
        await message.answer("Откройте Пост внутри аккаунта")
        return
    await _save_post_message(message, int(account_id), "en")


@router.callback_query(MenuCB.filter(F.a == "post_prev"))
async def cb_prev(query: CallbackQuery, callback_data: MenuCB) -> None:
    if not callback_data.i:
        await query.answer("Откройте Пост внутри аккаунта", show_alert=True)
        return
    ru = await ctx.store.get_post(callback_data.i, "ru")
    acc = await ctx.store.get_account(callback_data.i)
    label = escape(acc.label) if acc else f"#{callback_data.i}"
    chats = await ctx.store.list_chats(enabled_only=True)
    lines = [
        prompt_html(
            "Превью RU",
            f"Аккаунт: <b>{label}</b>\nПодстановка тега по чатам:",
            "search",
        )
    ]
    for chat in chats[:12]:
        text, _ = render_post(ru.text, entities_loads(ru.entities_json), chat.tag)
        snippet = escape(text.replace("\n", " ")[:80])
        tag = escape(chat.tag or "нет тега")
        lines.append(f"{pe('pin')} <b>{escape(chat.display_name)}</b> [{tag}]: {snippet}")
    if not chats:
        lines.append("<i>Нет чатов</i>")
    await query.answer()
    await query.message.answer("\n".join(lines), parse_mode="HTML")
