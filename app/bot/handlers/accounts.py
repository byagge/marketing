from __future__ import annotations

from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import MenuCB, account_kb, accounts_kb, cancel_kb, confirm_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import AccountFile, AddAccount
from app.ui.screens import account_html, accounts_html, prompt_html
from app.config import SESSIONS_DIR
from app.context import ctx
from app.jobs import runtime
from app.tg.client import inspect_session
from app.utils.sessions import detect_session_kind

router = Router()

_cards: dict[int, tuple[int, int]] = {}


async def _account_payload(acc):
    ru = await ctx.store.get_post(acc.id, "ru")
    en = await ctx.store.get_post(acc.id, "en")
    return account_html(acc, ru, en), account_kb(acc, runtime.is_running("setup", acc.id))


def remember_account_card(account_id: int, message: Message | None) -> None:
    if message is None or message.chat is None:
        return
    loc = (message.chat.id, message.message_id)
    for aid, existing in list(_cards.items()):
        if existing == loc and aid != account_id:
            _cards.pop(aid, None)
    _cards[account_id] = loc


async def show_account_card(event: CallbackQuery | Message, acc) -> None:
    text, markup = await _account_payload(acc)
    await safe_edit(event, text, markup)
    if isinstance(event, CallbackQuery):
        remember_account_card(acc.id, event.message)


async def refresh_account_card(account_id: int) -> None:
    loc = _cards.get(account_id)
    bot = getattr(ctx, "bot", None)
    if not loc or not bot:
        return
    acc = await ctx.store.get_account(account_id)
    if not acc:
        return
    text, markup = await _account_payload(acc)
    chat_id, message_id = loc
    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        try:
            await bot.edit_message_caption(
                caption=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    except Exception:
        pass


@router.callback_query(MenuCB.filter(F.a == "accounts"))
async def cb_accounts(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    accounts = await ctx.store.list_accounts()
    await safe_edit(query, accounts_html(accounts), accounts_kb(accounts, callback_data.p))


@router.callback_query(MenuCB.filter(F.a == "acc"))
async def cb_acc(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    await show_account_card(query, acc)


@router.callback_query(MenuCB.filter(F.a == "acc_add"))
async def cb_acc_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAccount.session)
    await safe_edit(
        query,
        prompt_html(
            "Добавить аккаунт",
            "Пришлите файл <b>.session</b> (Telethon — для schedule).\n"
            "Если это Pyrogram — сохраню в слот sender.",
            "lock",
        ),
        cancel_kb(),
    )
    await ask_input(query, prompt_html("Файл", "Жду <b>.session</b>", "inbox"))


@router.message(AddAccount.session, F.document)
async def on_new_session(message: Message, state: FSMContext) -> None:
    doc = message.document
    if not doc or not (doc.file_name or "").lower().endswith(".session"):
        await message.answer("Нужен файл с расширением .session")
        return
    tmp = SESSIONS_DIR / f"_upload_{doc.file_unique_id}.session"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    await message.bot.download(doc, destination=tmp)
    kind = detect_session_kind(tmp)
    await state.update_data(tmp=str(tmp), kind=kind, filename=doc.file_name or "account.session")
    await state.set_state(AddAccount.label)
    await ask_input(
        message,
        prompt_html("Имя", f"Файл принят (<code>{kind}</code>).\nИмя аккаунта в таблице:"),
    )


@router.message(AddAccount.label, F.text)
async def on_new_label(message: Message, state: FSMContext) -> None:
    label = (message.text or "").strip()
    if not label:
        await message.answer("Пустое имя")
        return
    data = await state.get_data()
    tmp = Path(data["tmp"])
    kind = data.get("kind") or "unknown"
    acc = await ctx.store.add_account(label)
    dest_dir = SESSIONS_DIR / str(acc.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if kind == "pyrogram":
        dest = dest_dir / "pyrogram.session"
        dest.write_bytes(tmp.read_bytes())
        await ctx.store.update_account(acc.id, pyrogram_session=str(dest))
        note = "Сохранён как Pyrogram. Для schedule загрузите Telethon session на карточке аккаунта."
    else:
        dest = dest_dir / "telethon.session"
        dest.write_bytes(tmp.read_bytes())
        fields = {"telethon_session": str(dest)}
        try:
            info = await inspect_session(dest)
            fields.update(
                user_id=info["user_id"],
                username=info["username"],
                phone=info["phone"],
            )
            note = f"Telethon: вошли как {info['first_name']} (@{info['username'] or '-'})"
        except Exception as e:
            note = f"Session сохранена, но не удалось открыть: {e}"
        await ctx.store.update_account(acc.id, **fields)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    await state.clear()
    acc = await ctx.store.get_account(acc.id)
    from html import escape as _esc

    if not acc:
        await message.answer("Аккаунт не найден")
        return
    text, markup = await _account_payload(acc)
    await finish_input(message, f"{_esc(note)}\n\n{text}", markup)


@router.callback_query(MenuCB.filter(F.a.in_({"acc_tl", "acc_pg"})))
async def cb_acc_file(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.update_data(account_id=callback_data.i)
    if callback_data.a == "acc_tl":
        await state.set_state(AccountFile.telethon)
        kind = "Telethon"
    else:
        await state.set_state(AccountFile.pyrogram)
        kind = "Pyrogram"
    await safe_edit(
        query,
        prompt_html("Session", f"Пришлите <b>{kind}</b> .session для этого аккаунта.", "lock"),
        cancel_kb(),
    )
    await ask_input(query, prompt_html("Файл", "Жду <b>.session</b>", "inbox"))


async def _save_named_session(message: Message, account_id: int, slot: str) -> None:
    doc = message.document
    acc = await ctx.store.get_account(account_id)
    if not acc:
        await message.answer("Аккаунт не найден")
        return
    dest_dir = SESSIONS_DIR / str(acc.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{slot}.session"
    await message.bot.download(doc, destination=dest)
    kind = detect_session_kind(dest)
    expected = "telethon" if slot == "telethon" else "pyrogram"
    if kind not in {expected, "unknown"}:
        await message.answer(f"Похоже, это {kind}, а ждали {expected}. Файл всё равно сохранён.")
    fields = {f"{slot}_session": str(dest)}
    extra = ""
    if slot == "telethon":
        try:
            info = await inspect_session(dest)
            fields.update(
                user_id=info["user_id"],
                username=info["username"],
                phone=info["phone"],
            )
            extra = f"\nВошли: {info['first_name']} (@{info['username'] or '-'})"
        except Exception as e:
            extra = f"\nНе открылась: {e}"
    await ctx.store.update_account(acc.id, **fields)
    acc = await ctx.store.get_account(acc.id)
    from html import escape as _esc

    if not acc:
        await message.answer("Аккаунт не найден")
        return
    text, markup = await _account_payload(acc)
    await finish_input(message, text + _esc(extra), markup)


@router.message(AccountFile.telethon, F.document)
async def on_tl(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if "account_id" not in data:
        await message.answer("Сессия ввода сброшена. Откройте аккаунт снова.")
        return
    await _save_named_session(message, int(data["account_id"]), "telethon")


@router.message(AccountFile.pyrogram, F.document)
async def on_pg(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if "account_id" not in data:
        await message.answer("Сессия ввода сброшена. Откройте аккаунт снова.")
        return
    await _save_named_session(message, int(data["account_id"]), "pyrogram")


@router.callback_query(MenuCB.filter(F.a == "acc_tok"))
async def cb_tok(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(AccountFile.bot_token)
    await state.update_data(account_id=callback_data.i)
    await safe_edit(
        query,
        prompt_html("Token", "Пришлите <b>bot token</b> этого аккаунта (Autoposter API).", "link"),
        cancel_kb(),
    )
    await ask_input(query, prompt_html("Token", "Жду token", "link"))


@router.message(AccountFile.bot_token, F.text)
async def on_tok(message: Message, state: FSMContext) -> None:
    token = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    acc = await ctx.store.update_account(int(data["account_id"]), sender_bot_token=token)
    try:
        await message.delete()
    except Exception:
        pass
    if not acc:
        await message.answer("Аккаунт не найден")
        return
    text, markup = await _account_payload(acc)
    await finish_input(message, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acc_sid"))
async def cb_sid(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(AccountFile.sender_id)
    await state.update_data(account_id=callback_data.i)
    await safe_edit(
        query,
        prompt_html(
            "Autoposter ID",
            "account_id, если аккаунт уже есть в sender.\nИли <code>-</code> чтобы очистить.",
            "cube",
        ),
        cancel_kb(),
    )
    await ask_input(query, prompt_html("ID", "Жду account_id", "cube"))


@router.message(AccountFile.sender_id, F.text)
async def on_sid(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value in {"-", "0", "нет"}:
        value = ""
    data = await state.get_data()
    await state.clear()
    acc = await ctx.store.update_account(int(data["account_id"]), sender_account_id=value)
    if not acc:
        await message.answer("Аккаунт не найден")
        return
    text, markup = await _account_payload(acc)
    await finish_input(message, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acc_ren"))
async def cb_ren(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.set_state(AccountFile.rename)
    await state.update_data(account_id=callback_data.i)
    await safe_edit(query, prompt_html("Имя", "Новое имя аккаунта:"), cancel_kb())
    await ask_input(query, prompt_html("Имя", "Жду имя"))


@router.message(AccountFile.rename, F.text)
async def on_ren(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    acc = await ctx.store.update_account(int(data["account_id"]), label=(message.text or "").strip())
    if not acc:
        await message.answer("Аккаунт не найден")
        return
    text, markup = await _account_payload(acc)
    await finish_input(message, text, markup)


@router.callback_query(MenuCB.filter(F.a == "acc_del"))
async def cb_del(query: CallbackQuery, callback_data: MenuCB) -> None:
    await safe_edit(
        query,
        prompt_html("Удалить", "Удалить аккаунт из системы? Session-файлы на диске останутся.", "warn"),
        confirm_kb(
            MenuCB(a="acc_del2", i=callback_data.i),
            MenuCB(a="acc", i=callback_data.i),
            yes_text="Удалить",
        ),
    )


@router.callback_query(MenuCB.filter(F.a == "acc_del2"))
async def cb_del2(query: CallbackQuery, callback_data: MenuCB) -> None:
    await ctx.store.delete_account(callback_data.i)
    accounts = await ctx.store.list_accounts()
    await safe_edit(query, accounts_html(accounts), accounts_kb(accounts))
