from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import MenuCB, cancel_kb, sender_kb
from app.bot.render import ask_input, finish_input, safe_edit
from app.bot.states import EditPost, EditSettings
from app.config import get_settings
from app.context import ctx
from app.jobs import LogSink, runtime
from app.sender_api import SenderAPI, SenderAPIError
from app.tg.sender_push import normalize_multiline
from app.ui.screens import prompt_html, sender_html
from app.utils.entities import entities_dumps, entities_loads, from_aiogram_message

router = Router()


async def _text() -> str:
    return sender_html(await ctx.store.sender_settings())


@router.callback_query(MenuCB.filter(F.a == "sender"))
async def cb_sender(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(query, await _text(), sender_kb())


def _parse_range(raw: str) -> tuple[int, int] | None:
    raw = raw.replace("–", "-").replace("—", "-")
    parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    if "-" in raw and len(parts) == 1:
        parts = [p.strip() for p in raw.split("-") if p.strip()]
    if len(parts) == 1 and parts[0].isdigit():
        n = int(parts[0])
        return n, n
    if len(parts) == 2 and parts[0].lstrip("-").isdigit() and parts[1].lstrip("-").isdigit():
        a, b = int(parts[0]), int(parts[1])
        return min(a, b), max(a, b)
    return None


@router.callback_query(MenuCB.filter(F.a.in_({"s_between", "s_cycle", "s_pchat"})))
async def cb_range(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    mapping = {
        "s_between": ("between", EditSettings.between, "min max, например 30 90"),
        "s_cycle": ("cycle", EditSettings.cycle, "min max в секундах, например 300 600"),
        "s_pchat": ("per_chat", EditSettings.per_chat, "min max, например 3600 3600"),
    }
    key, st, hint = mapping[callback_data.a]
    await state.set_state(st)
    await state.update_data(field=key)
    text = prompt_html("Интервал", hint, "clock")
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(EditSettings.between, F.text)
@router.message(EditSettings.cycle, F.text)
@router.message(EditSettings.per_chat, F.text)
async def on_range(message: Message, state: FSMContext) -> None:
    parsed = _parse_range(message.text or "")
    if not parsed:
        await message.answer("Формат: 30 90")
        return
    data = await state.get_data()
    field = data["field"]
    lo, hi = parsed
    await ctx.store.update_sender_settings(**{f"{field}_min": lo, f"{field}_max": hi})
    await state.clear()
    await finish_input(message, await _text(), sender_kb())


@router.callback_query(MenuCB.filter(F.a == "s_par"))
async def cb_par(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditSettings.parallel)
    text = prompt_html("Parallel", "Сколько чатов одновременно (число ≥ 1):", "users")
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(EditSettings.parallel, F.text)
async def on_par(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("Число ≥ 1")
        return
    await ctx.store.update_sender_settings(parallel=int(raw))
    await state.clear()
    await finish_input(message, await _text(), sender_kb())


@router.callback_query(MenuCB.filter(F.a == "s_cloak"))
async def cb_cloak(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditPost.cloak)
    text = prompt_html(
        "Клоакинг",
        "Текст на все аккаунты при настройке. "
        "Можно premium emoji и Enter (переносы).\n"
        "<code>-</code> чтобы выключить.\n\n"
        "Важно: настройка пишется в каждый sender-аккаунт отдельно "
        "(без глобального apply-all).",
        "shield",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(EditPost.cloak, F.text | F.caption)
async def on_cloak(message: Message, state: FSMContext) -> None:
    raw = (message.text or message.caption or "").strip()
    if raw in {"-", "off", "выкл"}:
        await ctx.store.update_sender_settings(
            cloak_enabled=False,
            cloak_text="",
            cloak_entities_json="[]",
        )
    else:
        text, entities = from_aiogram_message(message)
        text = normalize_multiline(text)
        await ctx.store.update_sender_settings(
            cloak_enabled=True,
            cloak_text=text,
            cloak_entities_json=entities_dumps(entities),
        )
    await state.clear()
    await finish_input(message, await _text(), sender_kb())


@router.callback_query(MenuCB.filter(F.a == "s_cloak_all"))
async def cb_cloak_all(query: CallbackQuery) -> None:
    ss = await ctx.store.sender_settings()
    if not ss.cloak_enabled or not (ss.cloak_text or "").strip():
        await query.answer("Сначала задайте текст клоакинга", show_alert=True)
        return
    if runtime.is_running("cloak_all", 0):
        await query.answer("Уже запущено", show_alert=True)
        return
    await query.answer("Применяю…")

    async def _job():
        settings = get_settings()
        api = SenderAPI(settings.sender_api_url, settings.sender_api_key)
        job = await ctx.store.create_job("cloak_all", None)
        log = LogSink(ctx.store, job.id, query.bot, query.from_user.id)
        cloak_text = normalize_multiline(ss.cloak_text)
        ents = entities_loads(ss.cloak_entities_json)
        ok = err = skip = 0
        await log.emit("Клоакинг → каждый sender-аккаунт отдельно…")
        for acc in await ctx.store.list_accounts():
            if not acc.has_sender:
                skip += 1
                continue
            sender_id = (acc.sender_account_id or "").strip()
            if not sender_id:
                skip += 1
                await log.emit(f"Пропуск {acc.label}: нет Sender ID", "error")
                continue
            try:
                await api.put_cloak(sender_id, True, cloak_text, entities=ents)
                ok += 1
                await log.emit(f"✓ {acc.label}")
            except SenderAPIError as e:
                err += 1
                await log.emit(f"✗ {acc.label}: {e}", "error")
            except Exception as e:
                err += 1
                await log.emit(f"✗ {acc.label}: {type(e).__name__}: {e}", "error")
        report = f"ok={ok}, ошибок={err}, пропуск={skip}"
        await ctx.store.finish_job(job.id, "done", report)
        await log.emit("Клоакинг применён. " + report)

    try:
        runtime.spawn("cloak_all", 0, _job())
    except RuntimeError as e:
        await query.answer(str(e), show_alert=True)
        return
    await safe_edit(query, await _text(), sender_kb())


@router.callback_query(MenuCB.filter(F.a == "s_keep"))
async def cb_keep(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditSettings.keep_ids)
    text = prompt_html(
        "Keep ids",
        "Дополнительные id через запятую — leave их не трогает. Каталог и так сохраняется.",
        "lock",
    )
    await safe_edit(query, text, cancel_kb())
    await ask_input(query, text)


@router.message(EditSettings.keep_ids, F.text)
async def on_keep(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw in {"-", "нет"}:
        raw = ""
    await ctx.store.update_sender_settings(keep_extra_ids=raw)
    await state.clear()
    await finish_input(message, await _text(), sender_kb())
