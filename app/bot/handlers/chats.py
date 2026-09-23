from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import MenuCB, cancel_kb, chat_kb, chats_kb, confirm_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import EditChat
from app.context import ctx
from app.tg.resolve import refresh_chat_titles_quiet, resolve_target
from app.ui.screens import chat_html, chats_html, prompt_html

router = Router()


@router.callback_query(MenuCB.filter(F.a == "chats"))
async def cb_chats(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Названия из Telegram…")
    await refresh_chat_titles_quiet(ctx.store)
    chats = await ctx.store.list_chats()
    await safe_edit(query, chats_html(chats), chats_kb(chats, callback_data.p))


@router.callback_query(MenuCB.filter(F.a == "chat"))
async def cb_chat(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    from app.tg.join_presets import apply_preset_fields

    fields = apply_preset_fields(chat)
    if fields:
        chat = await ctx.store.update_chat(chat.id, **fields) or chat
    await safe_edit(query, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_add"))
async def cb_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditChat.add)
    text = prompt_html(
        "Добавить чат",
        "Перешлите сообщение из чата или пришлите id / @username.\n"
        "Потом на карточке: тип, язык, тег, интервал.",
        "users",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


def _extract_chat(message: Message) -> tuple[str, str, str] | None:
    src = message.forward_from_chat or message.sender_chat
    if src is not None:
        title = getattr(src, "title", None) or getattr(src, "full_name", None) or str(src.id)
        username = getattr(src, "username", None) or ""
        return str(src.id), title, username
    text = (message.text or "").strip()
    if not text:
        return None
    if text.startswith("https://t.me/"):
        text = text.split("t.me/", 1)[1].split("/")[0]
    username = text[1:] if text.startswith("@") else ""
    return text, text, username


def _entity_query(chat_id: str, username: str) -> int | str:
    if username:
        return username
    raw = (chat_id or "").strip()
    if raw.startswith("@"):
        return raw[1:]
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


@router.message(EditChat.add)
async def on_add(message: Message, state: FSMContext) -> None:
    parsed = _extract_chat(message)
    if not parsed:
        await message.answer("Не понял чат. Перешлите сообщение из него.")
        return
    chat_id, title, username = parsed
    resolved = await resolve_target(ctx.store, _entity_query(chat_id, username))
    if resolved:
        chat_id = resolved["chat_id"] or chat_id
        title = resolved["title"] or title
        username = resolved["username"] or username
    existing = await ctx.store.get_chat_by_tg(chat_id)
    if existing:
        if resolved:
            existing = await ctx.store.update_chat(
                existing.id,
                title=title,
                username=username or existing.username,
            ) or existing
        await state.clear()
        await finish_input(message, chat_html(existing), chat_kb(existing))
        return
    chat = await ctx.store.add_chat(title=title, chat_id=chat_id, username=username)
    await state.clear()
    await finish_input(message, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_kind"))
async def cb_kind(query: CallbackQuery, callback_data: MenuCB) -> None:
    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    nxt = "sender" if chat.kind == "schedule" else "schedule"
    chat = await ctx.store.update_chat(chat.id, kind=nxt)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    await safe_edit(query, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_lang"))
async def cb_lang(query: CallbackQuery, callback_data: MenuCB) -> None:
    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    nxt = "en" if chat.lang == "ru" else "ru"
    chat = await ctx.store.update_chat(chat.id, lang=nxt)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    await safe_edit(query, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_on"))
async def cb_on(query: CallbackQuery, callback_data: MenuCB) -> None:
    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    chat = await ctx.store.update_chat(chat.id, enabled=0 if chat.enabled else 1)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    await safe_edit(query, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_tag"))
async def cb_tag(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(EditChat.tag)
    await state.update_data(chat_pk=callback_data.i)
    await safe_edit(query, prompt_html("Тег", "@username или <code>-</code>", "pin"), cancel_kb())
    await ask_input(query, prompt_html("Тег", "Жду тег / гарант", "pin"))


@router.message(EditChat.tag, F.text)
async def on_tag(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    tag = (message.text or "").strip()
    if tag in {"-", "нет", "0"}:
        tag = ""
    chat = await ctx.store.update_chat(int(data["chat_pk"]), tag=tag)
    if not chat:
        await message.answer("Чат не найден")
        return
    await finish_input(message, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_int"))
async def cb_int(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(EditChat.interval)
    await state.update_data(chat_pk=callback_data.i)
    await safe_edit(query, prompt_html("Интервал", "Минуты для schedule, обычно <b>60</b>.", "clock"), cancel_kb())
    await ask_input(query, prompt_html("Интервал", "Жду число минут", "clock"))


@router.message(EditChat.interval, F.text)
async def on_int(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 720):
        await message.answer("Число от 1 до 720")
        return
    data = await state.get_data()
    await state.clear()
    chat = await ctx.store.update_chat(int(data["chat_pk"]), interval_minutes=int(raw))
    if not chat:
        await message.answer("Чат не найден")
        return
    await finish_input(message, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_title"))
async def cb_title(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(EditChat.title)
    await state.update_data(chat_pk=callback_data.i)
    await safe_edit(query, prompt_html("Название", "Новое название чата:"), cancel_kb())
    await ask_input(query, prompt_html("Название", "Жду название"))


@router.message(EditChat.title, F.text)
async def on_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    chat = await ctx.store.update_chat(int(data["chat_pk"]), title=(message.text or "").strip())
    if not chat:
        await message.answer("Чат не найден")
        return
    await finish_input(message, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_invite"))
async def cb_invite(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(EditChat.invite)
    await state.update_data(chat_pk=callback_data.i)
    text = prompt_html(
        "Ссылка вступления",
        "https://t.me/+… / joinchat / @username\n"
        "или <code>-</code> чтобы очистить.\n"
        "Нужна для автовступления аккаунтов.",
        "link",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(EditChat.invite, F.text)
async def on_invite(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    raw = (message.text or "").strip()
    if raw in {"-", "нет", "0", "clear"}:
        raw = ""
    chat = await ctx.store.update_chat(int(data["chat_pk"]), invite_link=raw)
    if not chat:
        await message.answer("Чат не найден")
        return
    # подтянуть пресет по новой ссылке (MARKET 404 и т.п.)
    from app.tg.join_presets import apply_preset_fields

    fields = apply_preset_fields(chat)
    if fields:
        chat = await ctx.store.update_chat(chat.id, **fields) or chat
    await finish_input(message, chat_html(chat), chat_kb(chat))


def _cycle(value: str, options: tuple[str, ...]) -> str:
    cur = value or options[0]
    try:
        idx = options.index(cur)
    except ValueError:
        idx = 0
    return options[(idx + 1) % len(options)]


@router.callback_query(MenuCB.filter(F.a == "chat_cap"))
async def cb_cap(query: CallbackQuery, callback_data: MenuCB) -> None:
    from app.tg.join_presets import CAPTCHA_KIND_CYCLE

    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    nxt = _cycle(chat.captcha_kind or "auto", CAPTCHA_KIND_CYCLE)
    chat = await ctx.store.update_chat(chat.id, captcha_kind=nxt)
    await safe_edit(query, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_jmode"))
async def cb_jmode(query: CallbackQuery, callback_data: MenuCB) -> None:
    from app.tg.join_presets import JOIN_MODE_CYCLE

    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    nxt = _cycle(chat.join_mode or "direct", JOIN_MODE_CYCLE)
    chat = await ctx.store.update_chat(chat.id, join_mode=nxt)
    await safe_edit(query, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_after"))
async def cb_after(query: CallbackQuery, callback_data: MenuCB) -> None:
    from app.tg.join_presets import AFTER_JOIN_CYCLE

    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    nxt = _cycle(chat.after_join or "none", AFTER_JOIN_CYCLE)
    chat = await ctx.store.update_chat(chat.id, after_join=nxt)
    await safe_edit(query, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_req"))
async def cb_req(query: CallbackQuery, callback_data: MenuCB) -> None:
    chat = await ctx.store.get_chat(callback_data.i)
    if not chat:
        await query.answer("Нет чата", show_alert=True)
        return
    chat = await ctx.store.update_chat(chat.id, is_join_request=0 if chat.is_join_request else 1)
    await safe_edit(query, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_gbot"))
async def cb_gbot(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(EditChat.garant_bot)
    await state.update_data(chat_pk=callback_data.i)
    text = prompt_html(
        "Гарант-бот",
        "@LustifyGarant_bot / @GUARD_LSA_BOT\n"
        "Бот, который выдаёт временную ссылку.\n"
        "<code>-</code> — очистить.",
        "robot",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(EditChat.garant_bot, F.text)
async def on_gbot(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    raw = (message.text or "").strip().lstrip("@")
    if raw in {"-", "нет", "0", "clear"}:
        raw = ""
    fields: dict = {"garant_bot": raw}
    if raw:
        fields["join_mode"] = "garant"
    chat = await ctx.store.update_chat(int(data["chat_pk"]), **fields)
    if not chat:
        await message.answer("Чат не найден")
        return
    await finish_input(message, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_abot"))
async def cb_abot(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(EditChat.after_bot)
    await state.update_data(chat_pk=callback_data.i)
    text = prompt_html(
        "Бот после вступления",
        "@LSA_GRNT_BOT — капча в боте\n"
        "или тот же гарант для кнопки «Я вступил».\n"
        "<code>-</code> — очистить.",
        "cube",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(EditChat.after_bot, F.text)
async def on_abot(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    raw = (message.text or "").strip().lstrip("@")
    if raw in {"-", "нет", "0", "clear"}:
        raw = ""
    chat = await ctx.store.update_chat(int(data["chat_pk"]), after_join_bot=raw)
    if not chat:
        await message.answer("Чат не найден")
        return
    await finish_input(message, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_rch"))
async def cb_rch(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(EditChat.require_channels)
    await state.update_data(chat_pk=callback_data.i)
    text = prompt_html(
        "Обязательные каналы",
        "Через запятую: <code>@market404chat</code> или ссылки.\n"
        "Подписка нужна, чтобы писать в чат.\n"
        "<code>-</code> — очистить.",
        "pin",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(EditChat.require_channels, F.text)
async def on_rch(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    raw = (message.text or "").strip()
    if raw in {"-", "нет", "0", "clear"}:
        raw = ""
    chat = await ctx.store.update_chat(int(data["chat_pk"]), require_channels=raw)
    if not chat:
        await message.answer("Чат не найден")
        return
    await finish_input(message, chat_html(chat), chat_kb(chat))


@router.callback_query(MenuCB.filter(F.a == "chat_del"))
async def cb_del(query: CallbackQuery, callback_data: MenuCB) -> None:
    await safe_edit(
        query,
        prompt_html("Удалить", "Удалить чат из каталога и его минутную таблицу?", "warn"),
        confirm_kb(
            MenuCB(a="chat_del2", i=callback_data.i),
            MenuCB(a="chat", i=callback_data.i),
            yes_text="Удалить",
        ),
    )


@router.callback_query(MenuCB.filter(F.a == "chat_del2"))
async def cb_del2(query: CallbackQuery, callback_data: MenuCB) -> None:
    await ctx.store.delete_chat(callback_data.i)
    chats = await ctx.store.list_chats()
    await safe_edit(query, chats_html(chats), chats_kb(chats))
