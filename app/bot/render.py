from __future__ import annotations

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InputMediaPhoto,
    Message,
)

from app.bot.keyboards import input_cancel_keyboard, panel_keyboard


def post_summary(lang: str, text: str, entities: list, photo_path: str) -> str:
    n = len(text)
    emoji = sum(1 for e in entities if "emoji" in str(e.get("type")))
    photo = "есть" if photo_path else "нет"
    preview = text.replace("\n", " ")
    if len(preview) > 180:
        preview = preview[:177] + "…"
    return (
        f"{lang.upper()}: {n} символов | premium emoji: {emoji} | фото: {photo}\n"
        f"{preview or '—'}"
    )


def _msg(event: Message | CallbackQuery) -> Message | None:
    if isinstance(event, CallbackQuery):
        return event.message
    return event


def _has_media(message: Message | None) -> bool:
    if message is None:
        return False
    return bool(message.photo or message.document or message.video or message.animation)


def _caption(text: str) -> str:
    if len(text) <= 1000:
        return text
    return text[:997] + "…"


async def _ack(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass


async def safe_edit(
    event: Message | CallbackQuery,
    text: str,
    reply_markup=None,
    *,
    html: bool = True,
    photo: bytes | None = None,
) -> None:
    kwargs: dict = {"reply_markup": reply_markup}
    if html:
        kwargs["parse_mode"] = ParseMode.HTML
    message = _msg(event)
    media = _has_media(message)

    if photo is not None and message is not None:
        def _file() -> BufferedInputFile:
            return BufferedInputFile(photo, filename="table.png")

        caption = _caption(text)
        try:
            if media:
                await message.edit_media(
                    InputMediaPhoto(
                        media=_file(),
                        caption=caption,
                        parse_mode=ParseMode.HTML if html else None,
                    ),
                    reply_markup=reply_markup,
                )
            else:
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.answer_photo(_file(), caption=caption, **kwargs)
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():
                try:
                    await message.answer_photo(_file(), caption=caption, **kwargs)
                except Exception:
                    await message.answer(text, **kwargs)
        except Exception:
            try:
                await message.answer_photo(_file(), caption=caption, **kwargs)
            except Exception:
                await message.answer(text, **kwargs)
        await _ack(event)
        return

    if isinstance(event, CallbackQuery) and message is not None:
        try:
            if media:
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.answer(text, **kwargs)
            else:
                await message.edit_text(text, **kwargs)
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():
                await message.answer(text, **kwargs)
        except Exception:
            await message.answer(text, **kwargs)
        await _ack(event)
        return

    if "reply_markup" not in kwargs or kwargs["reply_markup"] is None:
        kwargs["reply_markup"] = panel_keyboard()
    await event.answer(text, **kwargs)


async def ask_input(event: Message | CallbackQuery, text: str) -> None:
    if isinstance(event, CallbackQuery):
        await _ack(event)
        await event.message.answer(
            text,
            reply_markup=input_cancel_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    await event.answer(text, reply_markup=input_cancel_keyboard(), parse_mode=ParseMode.HTML)


async def finish_input(message: Message, text: str, markup, photo: bytes | None = None) -> None:
    await message.answer("Готово", reply_markup=panel_keyboard())
    if photo is not None:
        await message.answer_photo(
            BufferedInputFile(photo, filename="table.png"),
            caption=_caption(text),
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
        return
    await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)
