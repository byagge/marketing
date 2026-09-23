from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.accounts import show_account_card
from app.bot.keyboards import MenuCB, cancel_kb, confirm_kb, membership_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import FolderJoin
from app.context import ctx
from app.jobs import runtime
from app.jobs.join import (
    run_folder_join,
    run_join_all_accounts,
    run_join_chats,
    run_membership_check,
)
from app.ui.emoji import pe
from app.ui.screens import prompt_html

router = Router()

_missing_cache: dict[int, list[int]] = {}


def _membership_html(acc_label: str, report: dict) -> str:
    joined_n = report.get("joined_n", 0)
    missing = report.get("missing") or []
    no_link = report.get("no_link") or []
    lines = [
        f"{pe('users')} <b>Проверка чатов</b>",
        f"{pe('user')} {escape(acc_label)}",
        "",
        f"{pe('check')} уже в чатах: <b>{joined_n}</b>",
        f"{pe('warn')} не вступлен: <b>{len(missing)}</b>",
        f"{pe('block')} без ссылки/способа: <b>{len(no_link)}</b>",
        "",
    ]
    if missing:
        lines.append(f"{pe('pin')} <b>Не вступлен:</b>")
        for chat in missing[:20]:
            link = escape((chat.invite_link or chat.garant_bot or chat.username or "—")[:40])
            lines.append(f"· {escape(chat.display_name)} <code>{link}</code>")
        if len(missing) > 20:
            lines.append(f"… ещё {len(missing) - 20}")
        lines.append("")
    if no_link:
        lines.append(f"{pe('link')} <b>Нет способа вступления:</b>")
        for chat in no_link[:12]:
            lines.append(f"· {escape(chat.display_name)}")
        lines.append("")
    lines.append(
        f"{pe('info')} Invite / гарант-бот / капчи (quiz, «не бот», кнопки-числа). "
        f"Заявки — в конце очереди. Папка — кнопка на карточке аккаунта."
    )
    return "\n".join(lines)


@router.callback_query(MenuCB.filter(F.a == "acc_chats"))
async def cb_acc_chats(query: CallbackQuery, callback_data: MenuCB) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    if not acc.telethon_session:
        await query.answer("Нужен Telethon session", show_alert=True)
        return
    await query.answer("Проверяю…")
    report = await run_membership_check(
        ctx.store, acc.id, query.bot, query.from_user.id
    )
    if not report.get("ok"):
        await query.message.answer(f"Ошибка: {report.get('error')}")
        return
    missing = report.get("missing") or []
    _missing_cache[acc.id] = [c.id for c in missing]
    await safe_edit(
        query,
        _membership_html(acc.label, report),
        membership_kb(acc.id, len(missing)),
    )


@router.callback_query(MenuCB.filter(F.a.in_({"acc_join_all", "acc_join_miss"})))
async def cb_acc_join(query: CallbackQuery, callback_data: MenuCB) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    if not acc.telethon_session:
        await query.answer("Нужен Telethon session", show_alert=True)
        return
    if runtime.is_running("join_chats", acc.id):
        await query.answer("Уже идёт вступление", show_alert=True)
        return

    only_missing = callback_data.a == "acc_join_miss"
    chat_pks = _missing_cache.get(acc.id) if only_missing else None
    if only_missing and not chat_pks:
        await query.answer("Сначала нажмите «Проверка чатов»", show_alert=True)
        return

    label = "недостающие" if only_missing else "все чаты каталога"
    await safe_edit(
        query,
        prompt_html(
            "Вступление",
            f"Аккаунт <b>{escape(acc.label)}</b>\nВступить в: <b>{label}</b>?",
            "users",
        ),
        confirm_kb(
            MenuCB(a="acc_join_go", i=acc.id, p=1 if only_missing else 0),
            MenuCB(a="acc_chats", i=acc.id),
            yes_text="Вступить",
        ),
    )


@router.callback_query(MenuCB.filter(F.a == "acc_join_go"))
async def cb_acc_join_go(query: CallbackQuery, callback_data: MenuCB) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    if runtime.is_running("join_chats", acc.id):
        await query.answer("Уже запущено", show_alert=True)
        return

    only_missing = bool(callback_data.p)
    chat_pks = _missing_cache.get(acc.id) if only_missing else None
    await query.answer("Запускаю вступление")

    async def _job():
        await run_join_chats(
            ctx.store,
            acc.id,
            chat_pks,
            query.bot,
            query.from_user.id,
        )

    try:
        runtime.spawn("join_chats", acc.id, _job())
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)
        return
    await show_account_card(query, acc)


@router.callback_query(MenuCB.filter(F.a == "acc_folder"))
async def cb_folder(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    acc = await ctx.store.get_account(callback_data.i)
    if not acc:
        await query.answer("Нет аккаунта", show_alert=True)
        return
    if not acc.telethon_session:
        await query.answer("Нужен Telethon session", show_alert=True)
        return
    await state.set_state(FolderJoin.link)
    await state.update_data(account_id=acc.id)
    text = prompt_html(
        "Папка Telegram",
        "Пришлите ссылку вида\n"
        "<code>https://t.me/addlist/XXXX</code>\n\n"
        "Аккаунт вступит во все чаты папки и попробует решить капчи.",
        "folder",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(FolderJoin.link, F.text)
async def on_folder_link(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    account_id = int(data.get("account_id") or 0)
    link = (message.text or "").strip()
    if not link:
        await message.answer("Пустая ссылка")
        return
    if runtime.is_running("folder_join", account_id):
        await message.answer("Уже запущено")
        return

    async def _job():
        await run_folder_join(
            ctx.store, account_id, link, message.bot, message.chat.id
        )

    try:
        runtime.spawn("folder_join", account_id, _job())
    except RuntimeError as e:
        await message.answer(str(e))
        return
    acc = await ctx.store.get_account(account_id)
    if acc:
        from app.bot.handlers.accounts import _account_payload

        text, markup = await _account_payload(acc)
        await finish_input(
            message,
            prompt_html("Папка", "Запущено — логи придут сюда.", "folder") + "\n\n" + text,
            markup,
        )


@router.callback_query(MenuCB.filter(F.a == "join_all_acc"))
async def cb_join_all_acc(query: CallbackQuery) -> None:
    await safe_edit(
        query,
        prompt_html(
            "Вступление всех",
            "Все Telethon-аккаунты вступят во все чаты каталога "
            "(ссылка / гарант-бот / капчи / заявки в конце).",
            "users",
        ),
        confirm_kb(
            MenuCB(a="join_all_go"),
            MenuCB(a="setup"),
            yes_text="Запустить",
        ),
    )


@router.callback_query(MenuCB.filter(F.a == "join_all_go"))
async def cb_join_all_go(query: CallbackQuery) -> None:
    if runtime.is_running("join_all_accounts", 0):
        await query.answer("Уже запущено", show_alert=True)
        return
    await query.answer("Запускаю")

    async def _job():
        await run_join_all_accounts(ctx.store, query.bot, query.from_user.id)

    try:
        runtime.spawn("join_all_accounts", 0, _job())
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)
        return
    await query.message.answer("Массовое вступление запущено — логи придут сюда.")
