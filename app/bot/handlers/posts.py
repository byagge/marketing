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
from app.utils.templates import SHORT_POST_MAX_CHARS, render_post, short_lang

router = Router()

_POST_ACTIONS = {
    "post_ru": ("ru", EditPost.ru, False),
    "post_en": ("en", EditPost.en, False),
    "post_rus": ("ru_short", EditPost.ru_short, True),
    "post_ens": ("en_short", EditPost.en_short, True),
}


async def _posts_screen(account_id: int) -> tuple[str, object]:
    acc = await ctx.store.get_account(account_id)
    if not acc:
        return prompt_html("Пост", "Аккаунт не найден.", "warn"), None
    ru = await ctx.store.get_post(acc.id, "ru")
    en = await ctx.store.get_post(acc.id, "en")
    ru_s = await ctx.store.get_post(acc.id, "ru_short")
    en_s = await ctx.store.get_post(acc.id, "en_short")
    return posts_html(acc, ru, en, ru_s, en_s), posts_kb(acc.id)


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


@router.callback_query(MenuCB.filter(F.a.in_(set(_POST_ACTIONS))))
async def cb_set_post(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    if not callback_data.i:
        await query.answer("Откройте Пост внутри аккаунта", show_alert=True)
        return
    lang, st, is_short = _POST_ACTIONS[callback_data.a]
    await state.set_state(st)
    await state.update_data(lang=lang, account_id=callback_data.i, is_short=is_short)
    acc = await ctx.store.get_account(callback_data.i)
    label = escape(acc.label) if acc else f"#{callback_data.i}"
    title = "Короткий пост" if is_short else "Пост"
    lang_label = lang.replace("_short", "").upper() + (" коротк." if is_short else "")
    extra = (
        f"Лимит: <b>{SHORT_POST_MAX_CHARS}</b> символов.\n"
        if is_short
        else ""
    )
    text = prompt_html(
        f"{title} {lang_label}",
        f"Аккаунт: <b>{label}</b>\n"
        f"{extra}"
        "Пришлите одним сообщением. Текст, premium emoji и фото сохранятся "
        "только у этого аккаунта.\n"
        "Можно <code>{{GARANT}}</code>.",
        "mega",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


async def _save_post_message(
    message: Message,
    account_id: int,
    lang: str,
    *,
    is_short: bool = False,
) -> None:
    text, entities = from_aiogram_message(message)
    if is_short and len(text or "") > SHORT_POST_MAX_CHARS:
        await message.answer(
            f"Слишком длинно: {len(text)} / {SHORT_POST_MAX_CHARS} символов. "
            "Сократите текст и пришлите снова."
        )
        return
    photo_path = ""
    dest_dir = POSTS_DIR / str(account_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_stem = lang  # ru / en / ru_short / en_short
    if message.photo:
        dest = dest_dir / f"{file_stem}.jpg"
        await message.bot.download(message.photo[-1], destination=dest)
        photo_path = str(dest)
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        dest = dest_dir / f"{file_stem}_{message.document.file_name or 'file'}"
        await message.bot.download(message.document, destination=dest)
        photo_path = str(dest)
    await ctx.store.save_post(account_id, lang, text, entities, photo_path or None)
    screen, markup = await _posts_screen(account_id)
    await finish_input(message, screen, markup or cancel_kb())


async def _on_post_message(message: Message, state: FSMContext, *, default_lang: str) -> None:
    data = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await state.clear()
        await message.answer("Откройте Пост внутри аккаунта")
        return
    lang = data.get("lang") or default_lang
    is_short = bool(data.get("is_short")) or str(lang).endswith("_short")
    if is_short and not str(lang).endswith("_short"):
        lang = short_lang(str(lang))
    text, _entities = from_aiogram_message(message)
    if is_short and len(text or "") > SHORT_POST_MAX_CHARS:
        await message.answer(
            f"Слишком длинно: {len(text)} / {SHORT_POST_MAX_CHARS} символов. "
            "Сократите и пришлите снова (состояние сохранено)."
        )
        return
    await state.clear()
    await _save_post_message(message, int(account_id), str(lang), is_short=is_short)


@router.message(EditPost.ru, F.text | F.photo | F.caption)
async def on_ru(message: Message, state: FSMContext) -> None:
    await _on_post_message(message, state, default_lang="ru")


@router.message(EditPost.en, F.text | F.photo | F.caption)
async def on_en(message: Message, state: FSMContext) -> None:
    await _on_post_message(message, state, default_lang="en")


@router.message(EditPost.ru_short, F.text | F.photo | F.caption)
async def on_ru_short(message: Message, state: FSMContext) -> None:
    await _on_post_message(message, state, default_lang="ru_short")


@router.message(EditPost.en_short, F.text | F.photo | F.caption)
async def on_en_short(message: Message, state: FSMContext) -> None:
    await _on_post_message(message, state, default_lang="en_short")


@router.callback_query(MenuCB.filter(F.a == "post_prev"))
async def cb_prev(query: CallbackQuery, callback_data: MenuCB) -> None:
    if not callback_data.i:
        await query.answer("Откройте Пост внутри аккаунта", show_alert=True)
        return
    ru = await ctx.store.get_post(callback_data.i, "ru")
    ru_s = await ctx.store.get_post(callback_data.i, "ru_short")
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
        post = ru_s if chat.uses_short_text and (ru_s.text or "").strip() else ru
        text, _ = render_post(post.text, entities_loads(post.entities_json), chat.tag)
        snippet = escape(text.replace("\n", " ")[:80])
        tag = escape(chat.tag or "нет тега")
        kind = "кор." if chat.uses_short_text else "полн."
        lines.append(
            f"{pe('pin')} <b>{escape(chat.display_name)}</b> [{tag}|{kind}]: {snippet}"
        )
    if not chats:
        lines.append("<i>Нет чатов</i>")
    await query.answer()
    await query.message.answer("\n".join(lines), parse_mode="HTML")
