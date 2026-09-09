"""
Одно отложенное сообщение с повтором (Daily / Weekly / …).

Нужно:
- Telegram Premium
- Telethon >= 1.44 (`pip install -U "telethon>=1.44"`)

Telegram сам перепланирует сообщение после отправки.
Допустимые периоды (не каждые 30 мин — для этого every_half_hour.py):
  Daily, Weekly, Biweekly, Monthly, Quarterly, Semiannual, Yearly
"""

import asyncio
import inspect
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import telethon
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types


SESSION_NAME = "scheduler_session"
TIMEZONE = ZoneInfo("Asia/Bishkek")

REPEAT_OPTIONS = {
    "1": ("Daily (каждый день)", 86400),
    "2": ("Weekly (каждую неделю)", 7 * 86400),
    "3": ("Biweekly (раз в 2 недели)", 14 * 86400),
    "4": ("Monthly (раз в 30 дней)", 30 * 86400),
    "5": ("Quarterly (раз в 3 месяца)", 91 * 86400),
    "6": ("Semiannual (раз в полгода)", 182 * 86400),
    "7": ("Yearly (раз в год)", 365 * 86400),
}


def require_telethon_repeat_support():
    version = tuple(int(x) for x in telethon.__version__.split(".")[:2])
    sig = inspect.signature(functions.messages.SendMessageRequest.__init__)

    if "schedule_repeat_period" not in sig.parameters:
        raise SystemExit(
            f"Telethon {telethon.__version__} не знает schedule_repeat_period.\n"
            "Обнови: pip install -U \"telethon>=1.44\""
        )

    if version < (1, 44):
        print(
            f"Предупреждение: Telethon {telethon.__version__}. "
            "Рекомендуется >= 1.44."
        )


def ask_api_credentials():
    api_id = 31999582
    api_hash = "d1126aadf79c595b641181fd4d5df2ea"

    if not api_id:
        api_id = input("API_ID: ").strip()

    if not api_hash:
        api_hash = input("API_HASH: ").strip()

    return int(api_id), api_hash


def ask_repeat_period() -> tuple[str, int]:
    print()
    print("Период повтора (Premium):")
    for key, (label, seconds) in REPEAT_OPTIONS.items():
        print(f"  {key}. {label}  ({seconds} сек)")

    while True:
        choice = input("Выбор: ").strip()
        if choice in REPEAT_OPTIONS:
            return REPEAT_OPTIONS[choice]
        print("Введите номер из списка.")


def ask_first_schedule_time() -> datetime:
    now = datetime.now(TIMEZONE)

    print()
    print(f"Сейчас: {now.strftime('%d.%m.%Y %H:%M')} ({TIMEZONE})")
    print("Первая отправка:")
    print("  - Enter = завтра в 09:00")
    print("  - или ДД.ММ.ГГГГ ЧЧ:ММ")
    print("  - или ЧЧ:ММ (сегодня; если прошло — завтра)")

    raw = input("Время: ").strip()

    if not raw:
        first = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if first <= now + timedelta(seconds=30):
            first += timedelta(days=1)
        return first

    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=TIMEZONE)
            break
        except ValueError:
            dt = None

    if dt is None:
        try:
            clock = datetime.strptime(raw, "%H:%M")
            dt = now.replace(
                hour=clock.hour,
                minute=clock.minute,
                second=0,
                microsecond=0,
            )
            if dt <= now + timedelta(seconds=30):
                dt += timedelta(days=1)
        except ValueError as e:
            raise ValueError(
                "Формат: ДД.ММ.ГГГГ ЧЧ:ММ или ЧЧ:ММ"
            ) from e

    if dt <= now + timedelta(seconds=30):
        raise ValueError("Время должно быть в будущем.")

    return dt


def normalize_chat_target(raw: str):
    raw = raw.strip()

    if raw.startswith("https://t.me/"):
        raw = raw.replace("https://t.me/", "", 1)
    elif raw.startswith("http://t.me/"):
        raw = raw.replace("http://t.me/", "", 1)
    elif raw.startswith("t.me/"):
        raw = raw.replace("t.me/", "", 1)

    raw = raw.strip().strip("/")

    if "?" in raw:
        raw = raw.split("?", 1)[0]

    if raw.startswith("@"):
        return raw

    if raw.startswith("-") and raw[1:].isdigit():
        return int(raw)

    if raw.isdigit():
        return int(raw)

    return raw


def parse_post_link(link: str):
    link = link.strip()

    match_public = re.search(
        r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)",
        link
    )

    if match_public and match_public.group(1) != "c":
        username = match_public.group(1)
        message_id = int(match_public.group(2))
        return username, message_id, "public"

    match_private = re.search(
        r"(?:https?://)?t\.me/c/(\d+)/(\d+)",
        link
    )

    if match_private:
        internal_id = match_private.group(1)
        message_id = int(match_private.group(2))
        peer_id = int(f"-100{internal_id}")
        return peer_id, message_id, "private"

    raise ValueError(
        "Не понял ссылку на пост. Формат:\n"
        "https://t.me/channel/123\n"
        "или\n"
        "https://t.me/c/1234567890/123"
    )


async def get_available_chats(client: TelegramClient):
    dialogs = []

    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            dialogs.append(dialog)

    return dialogs


def format_dialog_line(index: int, dialog) -> str:
    entity = dialog.entity
    peer_id = utils.get_peer_id(entity)
    username = getattr(entity, "username", None)
    username_text = f"@{username}" if username else "-"

    if dialog.is_group:
        chat_type = "group"
    elif dialog.is_channel:
        chat_type = "channel"
    else:
        chat_type = "chat"

    return f"{index:>3}. {dialog.name} | {chat_type} | id={peer_id} | {username_text}"


async def choose_target_chat(client: TelegramClient):
    dialogs = await get_available_chats(client)

    print()
    print("Куда назначить пост:")
    print()

    for i, dialog in enumerate(dialogs, start=1):
        print(format_dialog_line(i, dialog))

    print()
    print("Можно ввести: номер / @username / t.me/... / id")
    print()

    raw = input("Целевой чат: ").strip()

    if raw.isdigit():
        number = int(raw)
        if 1 <= number <= len(dialogs):
            return dialogs[number - 1].entity

    normalized = normalize_chat_target(raw)

    if isinstance(normalized, int):
        for dialog in dialogs:
            if utils.get_peer_id(dialog.entity) == normalized:
                return dialog.entity
            if getattr(dialog.entity, "id", None) == abs(normalized):
                return dialog.entity

    try:
        return await client.get_entity(normalized)
    except Exception as e:
        raise ValueError(
            "Не удалось найти целевой чат. Лучше выбери номером из списка."
        ) from e


async def get_source_post(client: TelegramClient):
    print()
    post_link = input("Ссылка на исходный пост: ").strip()

    source_peer, message_id, _link_type = parse_post_link(post_link)

    try:
        source_entity = await client.get_entity(source_peer)
    except Exception as e:
        raise ValueError(
            "Не удалось открыть канал исходного поста. "
            "Аккаунт должен иметь доступ к этому каналу."
        ) from e

    message = await client.get_messages(source_entity, ids=message_id)

    if not message:
        raise ValueError("Пост не найден. Проверь ссылку и доступ к каналу.")

    return source_entity, message


def _extract_sent_message(updates):
    for update in getattr(updates, "updates", []) or []:
        msg = getattr(update, "message", None)
        if msg is not None:
            return msg
    return updates


async def copy_repeating_scheduled_post(
    client: TelegramClient,
    target_entity,
    source_message,
    schedule_time: datetime,
    repeat_period: int,
):
    """
    Копия без forward + schedule_date + schedule_repeat_period.
    Альбомы (sendMultiMedia) Telegram не поддерживает с repeat.
    """

    peer = await client.get_input_entity(target_entity)
    markup = source_message.reply_markup
    silent = source_message.silent

    while True:
        try:
            has_real_media = (
                source_message.media
                and not isinstance(source_message.media, types.MessageMediaWebPage)
            )

            if has_real_media:
                media = utils.get_input_media(source_message.media)
                result = await client(
                    functions.messages.SendMediaRequest(
                        peer=peer,
                        media=media,
                        message=source_message.message or "",
                        entities=source_message.entities,
                        silent=silent,
                        reply_markup=markup,
                        schedule_date=schedule_time,
                        schedule_repeat_period=repeat_period,
                    )
                )
            else:
                result = await client(
                    functions.messages.SendMessageRequest(
                        peer=peer,
                        message=source_message.message or "",
                        entities=source_message.entities,
                        silent=silent,
                        reply_markup=markup,
                        clear_draft=False,
                        no_webpage=not isinstance(
                            source_message.media, types.MessageMediaWebPage
                        ),
                        schedule_date=schedule_time,
                        schedule_repeat_period=repeat_period,
                    )
                )

            return _extract_sent_message(result)

        except FloodWaitError as e:
            print(f"FloodWait: ждём {e.seconds} сек.")
            await asyncio.sleep(e.seconds + 1)

        except RPCError:
            raise


async def main():
    require_telethon_repeat_support()

    api_id, api_hash = ask_api_credentials()
    client = TelegramClient(SESSION_NAME, api_id, api_hash)

    await client.start()

    me = await client.get_me()
    print(f"\nВошли как: {me.first_name} | id={me.id}")

    if not getattr(me, "premium", False):
        print()
        print("Внимание: у аккаунта нет Premium-флага.")
        print("Повтор Daily/Weekly — функция Premium; без неё сервер может отклонить запрос.")

    source_entity, source_message = await get_source_post(client)

    print()
    print("Исходный пост найден:")
    print(
        f"Канал: "
        f"{getattr(source_entity, 'title', None) or getattr(source_entity, 'username', None)}"
    )
    print(f"Message ID: {source_message.id}")

    if source_message.grouped_id:
        print()
        print("Внимание: это часть альбома.")
        print("Repeat можно назначить только на одно сообщение, не на весь альбом.")

    target_entity = await choose_target_chat(client)
    label, repeat_period = ask_repeat_period()
    schedule_time = ask_first_schedule_time()

    print()
    print("Будет одно scheduled-сообщение с автоповтором:")
    print(f"  первая отправка: {schedule_time.strftime('%d.%m.%Y %H:%M')}")
    print(f"  повтор: {label}")
    print()

    confirm = input("Запланировать? y/N: ").strip().lower()
    if confirm not in ("y", "yes", "д", "да"):
        print("Отменено.")
        await client.disconnect()
        return

    print()
    print("Ставлю repeating scheduled-копию...")

    try:
        result = await copy_repeating_scheduled_post(
            client=client,
            target_entity=target_entity,
            source_message=source_message,
            schedule_time=schedule_time,
            repeat_period=repeat_period,
        )

        scheduled_id = getattr(result, "id", None)
        print(
            f"OK -> {schedule_time.strftime('%d.%m.%Y %H:%M')} | "
            f"repeat={repeat_period}s | scheduled id={scheduled_id}"
        )
        print()
        print(
            "Готово. После отправки Telegram сам поставит следующее "
            "на +period (пока не удалишь из отложенных)."
        )

    except RPCError as e:
        print()
        print(f"Ошибка: {type(e).__name__}: {e}")
        print(
            "Частые причины: нет Premium, альбом, "
            "или слишком много отложенных в чате."
        )

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
