from __future__ import annotations

from html import escape

from app.jobs import runtime
from app.models import Account, Chat, MinuteSlot, OnlinePingSettings
from app.ui.emoji import pe
from app.utils.entities import entities_loads
from app.utils.minutes import TableFullError, format_minute, period_for_interval, suggest_minute


def on_off(ok: bool) -> str:
    return f"{pe('check')} да" if ok else f"{pe('block')} нет"


async def home_html(store) -> str:
    accounts = await store.list_accounts()
    chats = await store.list_chats()
    sch = sum(1 for c in chats if c.is_schedule and c.enabled)
    snd = sum(1 for c in chats if c.kind == "sender" and c.enabled)
    bg = len(runtime.running_keys())
    bg_line = f"\n{pe('robot')} В фоне: <code>{escape(', '.join(runtime.running_keys()))}</code>" if bg else ""
    return (
        f"{pe('chart')} <b>Статистика</b>\n"
        f"Аккаунты: {len(accounts)} | Чаты: {len(chats)} | "
        f"Schedule: {sch} | Sender: {snd} | Фон: {bg}"
        f"{bg_line}\n\n"
        f"{pe('cube')} <b>Модуль настройки маркетинга</b>\n"
        f"Выберите действие:"
    )


def info_html() -> str:
    return (
        f"{pe('info')} <b>Marketing</b> 1.0.0\n"
        f"{pe('folder')} <b>Last Update:</b> 20.09.2026\n"
        f"{pe('at')} <b>Поддержка:</b> @arxixx\n\n"
        f"{pe('pin')} Пост задаётся отдельно у каждого аккаунта.\n"
        f"{pe('pin')} Schedule и sender независимы: сбой чата schedule "
        f"не блокирует sender.\n"
        f"{pe('star')} Без Premium — без schedule_repeat; суточный cron "
        f"переназначает слоты. Premium — Telegram daily repeat.\n"
        f"{pe('robot')} Настройка аккаунта идёт в фоне — можно открыть другой."
    )


def accounts_html(accounts: list[Account]) -> str:
    if not accounts:
        return (
            f"{pe('user')} <b>Аккаунты</b>\n\n"
            f"<i>Пока пусто. Нажмите «Добавить session» — загрузите .session.</i>"
        )
    lines = [f"{pe('user')} <b>Аккаунты</b> ({len(accounts)})\n"]
    for acc in accounts:
        live = pe("check") if acc.has_telethon else pe("block")
        run = f" {pe('robot')} фон" if runtime.is_running("setup", acc.id) else ""
        uname = f"@{acc.username}" if acc.username else escape(acc.label)
        lines.append(
            f"{live} <b>{escape(uname)}</b> <code>#{acc.id}</code>{run}\n"
            f"   статус: <code>{escape(acc.status)}</code>"
        )
    return "\n".join(lines)


def account_html(acc: Account, ru=None, en=None) -> str:
    uname = f"@{acc.username}" if acc.username else "—"
    run = runtime.is_running("setup", acc.id)
    err = f"\n{pe('warn')} <b>Ошибка:</b> {escape(acc.last_error)}" if acc.last_error else ""
    post_line = ""
    if ru is not None and en is not None:
        post_line = (
            f"\n{pe('mega')} Пост RU: {on_off(bool((ru.text or '').strip()))} | "
            f"EN: {on_off(bool((en.text or '').strip()))}"
        )

    if acc.sender_account_id:
        sender_mode = (
            f"{pe('cube')} <b>Sender:</b> связан по ID "
            f"<code>{escape(acc.sender_account_id)}</code>\n"
            f"   {pe('check')} session заново <b>не</b> загружается — "
            f"используем уже существующий аккаунт в Autoposter"
        )
    elif acc.pyrogram_session and acc.sender_bot_token:
        sender_mode = (
            f"{pe('link')} <b>Sender:</b> готов создать новый "
            f"(Pyrogram + token)\n"
            f"   {pe('info')} если аккаунт уже есть в Autoposter — "
            f"лучше укажите <b>Sender ID</b>"
        )
    elif acc.pyrogram_session or acc.sender_bot_token:
        missing = []
        if not acc.pyrogram_session:
            missing.append("Pyrogram")
        if not acc.sender_bot_token:
            missing.append("token")
        sender_mode = (
            f"{pe('link')} <b>Sender:</b> неполный "
            f"(нужен {' + '.join(missing)} или Sender ID)"
        )
    else:
        sender_mode = (
            f"{pe('cube')} <b>Sender:</b> не задан\n"
            f"   {pe('pin')} уже есть в Autoposter → кнопка <b>Sender ID</b>\n"
            f"   {pe('pin')} новый → Pyrogram + <b>Sender token</b>"
        )

    return (
        f"{pe('user')} <b>{escape(acc.label)}</b>\n\n"
        f"{pe('at')} {escape(uname)}\n"
        f"{pe('term')} tg_id: <code>{acc.user_id or '—'}</code>\n"
        f"{pe('bookmark')} статус: <code>{escape(acc.status)}</code>"
        f"{' ' + pe('robot') + ' в фоне' if run else ''}"
        f"{post_line}\n\n"
        f"{pe('lock')} Telethon: {on_off(bool(acc.telethon_session))}\n"
        f"{pe('star')} Premium: {on_off(bool(acc.is_premium))}"
        f"{'' if acc.is_premium else ' · суточный cron без repeat'}\n"
        f"{pe('monitor')} Pyrogram: {on_off(bool(acc.pyrogram_session))}\n"
        f"{pe('up')} Online ping: {on_off(bool(acc.online_ping_enabled))}"
        f"{'' if acc.telethon_session else ' (нужен Telethon)'}\n\n"
        f"{sender_mode}"
        f"{err}\n\n"
        f"{pe('info')} Schedule — чаты из каталога. "
        f"Sender — все остальные диалоги аккаунта."
    )


def online_html(cfg: OnlinePingSettings, accounts: list[Account]) -> str:
    with_tl = [a for a in accounts if a.telethon_session]
    on_n = sum(1 for a in with_tl if a.online_ping_enabled)
    off_n = len(with_tl) - on_n
    lines = [
        f"{pe('star')} <b>Online ping</b>\n",
        f"Кратко ставит аккаунт Online, чтобы last-seen "
        f"не был «был месяц назад».\n",
        f"{pe('check') if cfg.enabled else pe('block')} глобально: "
        f"<b>{'вкл' if cfg.enabled else 'выкл'}</b>",
        f"{pe('clock')} интервал: <b>{cfg.hours:g} ч</b> ± {cfg.jitter_sec // 60} мин",
        f"{pe('pin')} hold Online: <b>{cfg.hold_seconds:g} с</b>",
    ]
    if cfg.last_at:
        lines.append(f"{pe('bookmark')} последний: <code>{escape(cfg.last_at)}</code>")
    if cfg.next_at:
        lines.append(f"{pe('up')} следующий: <code>{escape(cfg.next_at)}</code>")
    lines.append("")
    lines.append(
        f"{pe('user')} Telethon-аккаунты: <b>{len(with_tl)}</b> "
        f"(ping вкл: {on_n}, выкл: {off_n})"
    )
    if with_tl:
        lines.append("")
        for acc in with_tl[:12]:
            mark = pe("check") if acc.online_ping_enabled else pe("block")
            lines.append(f"{mark} {escape(acc.label)}")
        if len(with_tl) > 12:
            lines.append(f"… и ещё {len(with_tl) - 12}")
    lines.append("")
    lines.append(
        f"{pe('info')} Пер-аккаунт: карточка аккаунта → "
        f"<b>Online ping: вкл/выкл</b>."
    )
    return "\n".join(lines)


def posts_html(acc: Account, ru, en, ru_short=None, en_short=None) -> str:
    delivery = (ru.delivery if ru else "post") or "post"

    def block(lang, post, *, short: bool = False) -> str:
        if post is None:
            return ""
        if (post.delivery or delivery) == "link":
            t_link = escape((post.link_url or "—")[:60])
            p_link = escape((post.link_photo_url or "—")[:60])
            title = f"{lang.upper()} коротк." if short else lang.upper()
            return (
                f"{pe('link')} <b>{title}</b> (ссылка)\n"
                f"{pe('pin')} текст: <code>{t_link}</code>\n"
                f"{pe('mega')} фото: <code>{p_link}</code>"
            )
        ents = entities_loads(post.entities_json)
        emoji_n = sum(1 for e in ents if "emoji" in str(e.get("type")))
        preview = escape((post.text or "").replace("\n", " ")[:140] or "—")
        photo = on_off(bool(post.photo_path))
        title = f"{lang.upper()} коротк." if short else lang.upper()
        limit = " | max 500" if short else ""
        return (
            f"{pe('mega')} <b>{title}</b>\n"
            f"{pe('pin')} {len(post.text or '')} симв.{limit} | premium: {emoji_n} | фото: {photo}\n"
            f"<i>{preview}</i>"
        )

    mode_line = (
        f"{pe('link')} режим: <b>ссылка</b> (пересылка с «Переслано из»)\n"
        if delivery == "link"
        else f"{pe('mega')} режим: <b>пост</b> (свой текст/фото)\n"
    )
    parts = [
        f"{pe('mega')} <b>Пост</b> {escape(acc.label)}\n"
        f"{pe('user')} Только этот аккаунт.\n"
        f"{mode_line}"
        f"{pe('pin')} <code>{{{{GARANT}}}}</code> — тег чата (режим пост).\n"
        f"{pe('info')} Короткий — до 500 симв. В чате: полный/короткий.\n"
        f"{pe('info')} Нет фото в чате → текст или текстовая ссылка.\n\n"
        + block("ru", ru)
        + "\n\n"
        + block("en", en)
    ]
    if ru_short is not None:
        parts.append("\n\n" + block("ru", ru_short, short=True))
    if en_short is not None:
        parts.append("\n\n" + block("en", en_short, short=True))
    return "".join(parts)


def chats_html(chats: list[Chat]) -> str:
    if not chats:
        return (
            f"{pe('users')} <b>Чаты</b>\n\n"
            f"<i>Пусто. Перешлите сообщение из чата или пришлите id / @username.</i>"
        )
    sch = sum(1 for c in chats if c.is_schedule)
    return (
        f"{pe('users')} <b>Чаты</b> ({len(chats)}). "
        f"{pe('clock')} schedule: {sch} | {pe('mega')} sender: {len(chats) - sch}\n"
        f"{pe('block')} — выключен из каталога."
    )


def chat_html(chat: Chat) -> str:
    from app.tg.join_presets import (
        AFTER_JOIN_LABEL,
        CAPTCHA_KIND_LABEL,
        JOIN_MODE_LABEL,
        match_preset,
    )

    kind = "schedule" if chat.is_schedule else "sender"
    invite = (chat.invite_link or "").strip() or "—"
    preset = match_preset(chat)
    preset_note = f"{pe('star')} пресет: да\n" if preset else ""
    return (
        f"{pe('users')} <b>{escape(chat.display_name)}</b>\n\n"
        f"{pe('term')} id: <code>{escape(chat.chat_id)}</code>\n"
        f"{pe('at')} {escape(chat.username or '—')}\n"
        f"{pe('link')} invite: <code>{escape(invite)}</code>\n"
        f"{preset_note}"
        f"{pe('cube')} тип: <b>{kind}</b>\n"
        f"{pe('bookmark')} язык: <b>{escape(chat.lang)}</b>\n"
        f"{pe('mega')} текст: <b>{'короткий' if chat.uses_short_text else 'полный'}</b>\n"
        f"{pe('mega')} медиа: <b>{'да' if chat.media_allowed else 'нет (только текст)'}</b>\n"
        f"{pe('pin')} тег/гарант: <code>{escape(chat.tag or '—')}</code>\n"
        f"{pe('clock')} интервал: <b>{chat.interval_minutes} мин</b>\n"
        f"{pe('shield')} капча: <b>{escape(CAPTCHA_KIND_LABEL.get(chat.captcha_kind, chat.captcha_kind))}</b>\n"
        f"{pe('users')} вступление: <b>{escape(JOIN_MODE_LABEL.get(chat.join_mode, chat.join_mode))}</b>\n"
        f"{pe('robot')} гарант-бот: <code>@{escape(chat.garant_bot or '—')}</code>\n"
        f"{pe('check')} после: <b>{escape(AFTER_JOIN_LABEL.get(chat.after_join, chat.after_join))}</b>\n"
        f"{pe('cube')} бот после: <code>@{escape(chat.after_join_bot or '—')}</code>\n"
        f"{pe('pin')} каналы: <code>{escape(chat.require_channels or '—')}</code>\n"
        f"{pe('warn')} заявка: <b>{'да' if chat.is_join_request else 'нет'}</b>\n"
        f"{pe('check') if chat.enabled else pe('block')} "
        f"{'включён' if chat.enabled else 'выключен'}\n\n"
        f"{pe('info')} Lustify/LSA подхватываются пресетом по id — "
        f"при создании связывать вручную не нужно."
    )


def table_index_html(n: int) -> str:
    return (
        f"{pe('clock')} <b>Таблица минут</b>\n"
        f"Schedule-чатов: <b>{n}</b>\n\n"
        f"Колонки — названия чатов, строки — минуты (0–59), в ячейке аккаунт.\n"
        f"Минуты всегда по <b>целому часу</b>, с равными промежутками.\n\n"
        f"{pe('clock')} <b>Выровнять минуты</b> — поправить текущие слоты + "
        f"schedule пачками по 5.\n"
        f"{pe('robot')} <b>Перенастроить все</b> — полная таблица + setup пачками.\n"
        f"{pe('stack')} <b>Только таблицу</b> — пересобрать минуты без Telegram."
    )


def table_html(chat: Chat, slots: list[MinuteSlot]) -> str:
    period = period_for_interval(chat.interval_minutes)
    occupied = [s.start_minute for s in slots]
    try:
        nxt = f":{format_minute(suggest_minute(period, occupied))}"
    except TableFullError:
        nxt = "нет мест"
    lines = [
        f"{pe('clock')} <b>{escape(chat.display_name)}</b>",
        f"интервал {chat.interval_minutes} мин | период {period}",
        f"{pe('up')} следующий свободный: <b>{nxt}</b>",
        "",
    ]
    if not slots:
        lines.append(f"{pe('inbox')} Пусто — первый аккаунт получит :00")
    else:
        for slot in slots:
            lines.append(
                f"{pe('pin')} <code>:{format_minute(slot.start_minute)}</code>  "
                f"<b>{escape(slot.account_label)}</b>"
            )
    return "\n".join(lines)


def sender_html(ss) -> str:
    cloak = escape((ss.cloak_text or "").replace("\n", " ↵ ")[:200] or "—")
    ents = 0
    try:
        from app.utils.entities import entities_loads

        ents = sum(
            1
            for e in entities_loads(getattr(ss, "cloak_entities_json", None))
            if "emoji" in str(e.get("type"))
        )
    except Exception:
        pass
    return (
        f"{pe('link')} <b>Sender / клоакинг</b>\n"
        f"Интервалы и клоакинг пишутся в каждый аккаунт отдельно при настройке.\n\n"
        f"{pe('clock')} between: <b>{ss.between_min}–{ss.between_max}</b> сек\n"
        f"{pe('stack')} cycle: <b>{ss.cycle_min}–{ss.cycle_max}</b> сек\n"
        f"{pe('pin')} per-chat: <b>{ss.per_chat_min}–{ss.per_chat_max}</b> сек\n"
        f"{pe('users')} parallel: <b>{ss.parallel}</b>\n"
        f"{pe('shield')} клоакинг: {on_off(ss.cloak_enabled)}"
        f"{f' | premium emoji: {ents}' if ents else ''}\n"
        f"<i>{cloak}</i>\n"
        f"{pe('users')} глобальные упоминания: "
        f"{on_off(bool(getattr(ss, 'mentions_enabled', False)))}\n\n"
        f"{pe('lock')} keep extra ids: <code>{escape(ss.keep_extra_ids or '—')}</code>"
    )


def setup_html() -> str:
    return (
        f"{pe('robot')} <b>Старт настройки</b>\n\n"
        f"Сначала все schedule-чаты из каталога, затем sender:\n"
        f"пост, интервалы, клоакинг, чаты; упоминания — по настройке Sender "
        f"(по умолчанию выкл).\n"
        f"На карточке аккаунта: Старт → Стоп, ошибка вернёт Старт.\n\n"
        f"Выберите действие:"
    )


def setup_ask_html(acc: Account, n_sch: int, n_snd: int, ru_ok: bool = False, en_ok: bool = False) -> str:
    if acc.sender_account_id:
        sender_line = (
            f"{pe('cube')} Sender: ID <code>{escape(acc.sender_account_id)}</code> "
            f"(без повторной загрузки)"
        )
    elif acc.has_sender:
        sender_line = f"{pe('link')} Sender: создаст новый в Autoposter (session+token)"
    else:
        sender_line = f"{pe('block')} Sender: не готов"

    return (
        f"{pe('robot')} Запустить настройку <b>{escape(acc.label)}</b>?\n\n"
        f"{pe('clock')} Schedule: <b>{n_sch}</b> (из каталога)\n"
        f"{pe('mega')} Sender-чаты каталога: <b>{n_snd}</b>\n"
        f"{pe('mega')} Пост RU: {on_off(ru_ok)} | EN: {on_off(en_ok)}\n"
        f"{pe('lock')} Telethon: {on_off(bool(acc.telethon_session))}\n"
        f"{sender_line}"
    )


def health_html() -> str:
    return (
        f"{pe('search')} <b>Проверка schedule</b>\n\n"
        f"Смотрим, не отпали ли отложенные сообщения в schedule-чатах."
    )


def reports_html(jobs) -> str:
    if not jobs:
        return f"{pe('chart')} <b>Отчёты</b>\n\n<i>Пока пусто.</i>"
    lines = [f"{pe('chart')} <b>Последние задачи</b>\n"]
    for job in jobs:
        name = escape(job.account_label or "—")
        snippet = escape((job.report or "").replace("\n", " ")[:90])
        mark = pe("check") if job.status == "done" else pe("warn") if job.status == "error" else pe("clock")
        lines.append(
            f"{mark} #{job.id} <b>{escape(job.kind)}</b> | {name}\n"
            f"   <code>{escape(job.status)}</code> {snippet}"
        )
    return "\n".join(lines)[:3900]


def leave_html() -> str:
    return (
        f"{pe('block')} <b>Выйти из чатов</b>\n\n"
        f"Аккаунт выйдет из всех диалогов, кроме каталога и Saved Messages.\n"
        f"Нужно подтверждение <code>YES</code>."
    )


def prompt_html(title: str, body: str, icon: str = "inbox") -> str:
    return f"{pe(icon)} <b>{title}</b>\n\n{body}"
