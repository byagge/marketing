from __future__ import annotations

import re
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards import MenuCB, cancel_kb, session_kind_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import MakeSession
from app.ui.emoji import pe
from app.ui.screens import prompt_html
from session_maker.api import normalize_phone, sanitize_session_name
from session_maker.bot_flow import begin_login, submit_code, submit_password
from session_maker.pending import clear_pending

router = Router()


def _uid(message: Message | CallbackQuery) -> int:
    user = message.from_user if isinstance(message, Message) else message.from_user
    assert user is not None
    return int(user.id)


@router.callback_query(MenuCB.filter(F.a == "mk_session"))
async def cb_mk_session(query: CallbackQuery, state: FSMContext) -> None:
    await clear_pending(_uid(query))
    await state.clear()
    await state.set_state(MakeSession.kind)
    await safe_edit(
        query,
        prompt_html(
            "Новая session",
            f"{pe('lock')} Telethon — для schedule\n"
            f"{pe('monitor')} Pyrogram — для sender\n\n"
            "Выберите тип:",
            "inbox",
        ),
        session_kind_kb(),
    )


@router.callback_query(MenuCB.filter(F.a.in_({"mk_tl", "mk_pg"})))
async def cb_kind(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    kind = "telethon" if callback_data.a == "mk_tl" else "pyrogram"
    await state.set_state(MakeSession.name)
    await state.update_data(kind=kind)
    await safe_edit(
        query,
        prompt_html(
            "Имя session",
            f"Тип: <b>{kind}</b>\n"
            "Имя файла без расширения (например <code>alexander</code>):",
            "pin",
        ),
        cancel_kb(),
    )
    await ask_input(
        query,
        prompt_html("Имя", "Жду имя session", "pin"),
    )


@router.message(MakeSession.name, F.text)
async def on_name(message: Message, state: FSMContext) -> None:
    name = sanitize_session_name(message.text or "")
    await state.update_data(session_name=name)
    await state.set_state(MakeSession.phone)
    await ask_input(
        message,
        prompt_html(
            "Телефон",
            f"Имя: <code>{name}</code>\n"
            "Номер в международном формате, например <code>+998901234567</code>",
            "at",
        ),
    )


@router.message(MakeSession.phone, F.text)
async def on_phone(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data.get("kind") or "telethon"
    name = data.get("session_name") or "account"
    try:
        phone = normalize_phone(message.text or "")
    except ValueError as e:
        await message.answer(str(e))
        return

    await message.answer(
        prompt_html("Код", "Отправляю код в Telegram…", "clock"),
        parse_mode=ParseMode.HTML,
    )
    try:
        login, status = await begin_login(
            user_id=_uid(message),
            kind=kind,
            session_name=name,
            phone=phone,
        )
    except Exception as e:
        await state.clear()
        await clear_pending(_uid(message))
        await message.answer(
            prompt_html("Ошибка", f"{type(e).__name__}: {e}", "warn"),
            parse_mode=ParseMode.HTML,
        )
        return

    # Already had a valid session file
    if login.session_path and login.session_path.exists() and login.client is None:
        await state.clear()
        await _send_session_file(message, login.session_path, status)
        return

    await state.set_state(MakeSession.code)
    await state.update_data(phone=phone)
    await ask_input(
        message,
        prompt_html(
            "Код",
            f"{status}\n\nПришлите код из Telegram / SMS.",
            "lock",
        ),
    )


@router.message(MakeSession.code, F.text)
async def on_code(message: Message, state: FSMContext) -> None:
    code = re.sub(r"\s+", "", message.text or "")
    if not code:
        await message.answer("Пришлите код")
        return
    try:
        status, path = await submit_code(_uid(message), code)
    except Exception as e:
        # Keep state so user can retry code; only clear on fatal
        err = f"{type(e).__name__}: {e}"
        if "Invalid" in type(e).__name__ or "invalid" in str(e).lower():
            await message.answer(
                prompt_html("Код", f"Неверный код. Попробуйте ещё раз.\n<code>{err}</code>", "warn"),
                parse_mode=ParseMode.HTML,
            )
            return
        if "Expired" in type(e).__name__ or "expired" in str(e).lower():
            await state.clear()
            await clear_pending(_uid(message))
            await message.answer(
                prompt_html("Код", "Код истёк. Начните заново из меню → Session.", "warn"),
                parse_mode=ParseMode.HTML,
            )
            return
        await state.clear()
        await clear_pending(_uid(message))
        await message.answer(
            prompt_html("Ошибка", err, "warn"),
            parse_mode=ParseMode.HTML,
        )
        return

    if status == "password":
        await state.set_state(MakeSession.password)
        await ask_input(
            message,
            prompt_html("2FA", "Нужен облачный пароль (двухэтапная аутентификация):", "shield"),
        )
        return

    await state.clear()
    if path is None or not path.exists():
        await message.answer("Session не создан")
        return
    await _send_session_file(message, path, status)


@router.message(MakeSession.password, F.text)
async def on_password(message: Message, state: FSMContext) -> None:
    password = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass
    try:
        note, path = await submit_password(_uid(message), password)
    except Exception as e:
        await state.clear()
        await clear_pending(_uid(message))
        await message.answer(
            prompt_html("Ошибка", f"{type(e).__name__}: {e}", "warn"),
            parse_mode=ParseMode.HTML,
        )
        return
    await state.clear()
    await _send_session_file(message, path, note)


async def _send_session_file(message: Message, path: Path, note: str) -> None:
    from app.bot.keyboards import main_menu

    data = path.read_bytes()
    doc = BufferedInputFile(data, filename=path.name)
    await message.answer_document(
        doc,
        caption=prompt_html("Session", f"{note}\n\nФайл: <code>{path.name}</code>", "check"),
        parse_mode=ParseMode.HTML,
    )
    await finish_input(
        message,
        prompt_html(
            "Готово",
            "Загрузите этот файл в карточку аккаунта "
            "(Telethon / Pyrogram) или сохраните у себя.",
            "inbox",
        ),
        main_menu(),
    )
