"""
Планировщик с выбором интервала в минутах.
Сетка от 09:start_minute, шаг N мин +1 мин на слот (как в every_half_hour).
"""

import asyncio
import inspect
import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import telethon
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types


SESSION_NAME = "scheduler_session"
TIMEZONE = ZoneInfo("Asia/Bishkek")

START_HOUR = 9
# Сколько часов сетки покрываем (как every_hour ~23).
COVER_HOURS = 23
# Лимит отложенных сообщений в одном чате (Telegram).
MAX_SCHEDULED = 99

# Telegram Premium: schedule_repeat_period (секунды).
REPEAT_OPTIONS = {
    "0": ("без repeat", None),
    "1": ("Daily (каждый день)", 86400),
    "2": ("Weekly (каждую неделю)", 7 * 86400),
    "3": ("Biweekly (раз в 2 недели)", 14 * 86400),
    "4": ("Monthly (раз в 30 дней)", 30 * 86400),
    "5": ("Quarterly (раз в 3 месяца)", 91 * 86400),
    "6": ("Semiannual (раз в полгода)", 182 * 86400),
    "7": ("Yearly (раз в год)", 365 * 86400),
}


def ask_api_credentials():
    api_id = 31999582
    api_hash = "d1126aadf79c595b641181fd4d5df2ea"

    if not api_id:
        api_id = input("API_ID: ").strip()

    if not api_hash:
        api_hash = input("API_HASH: ").strip()

    return int(api_id), api_hash


def ask_interval_minutes() -> int:
    while True:
        raw = input("Интервал в минутах (1–720): ").strip()

        if not raw.isdigit():
            print("Введите целое число, например: 5, 10, 15, 45")
            continue

        minutes = int(raw)

        if 1 <= minutes <= 720:
            return minutes

        print("Интервал должен быть от 1 до 720 минут.")


def ask_start_minute() -> int:
    while True:
        raw = input("Start minute от 00 до 59: ").strip()

        if not raw.isdigit():
            print("Введите число: 00, 05, 17, 59")
            continue

        minute = int(raw)

        if 0 <= minute <= 59:
            return minute

        print("Минута должна быть от 0 до 59.")


def posts_count_for_interval(interval_minutes: int) -> int:
    # Сколько слотов нужно на COVER_HOURS при шаге interval (+ дрейф +1 мин).
    needed = max(1, math.ceil((COVER_HOURS * 60) / interval_minutes))
    return min(needed, MAX_SCHEDULED)


def require_telethon_repeat_support():
    sig = inspect.signature(functions.messages.SendMessageRequest.__init__)
    if "schedule_repeat_period" not in sig.parameters:
        raise SystemExit(
            f"Telethon {telethon.__version__} не знает schedule_repeat_period.\n"
            "Обнови: pip install -U \"telethon>=1.44\""
        )


def ask_repeat_period() -> tuple[str, int | None]:
    print()
    print("Repeat (новая фича Telegram, Premium):")
    for key, (label, seconds) in REPEAT_OPTIONS.items():
        extra = "" if seconds is None else f"  ({seconds} сек)"
        print(f"  {key}. {label}{extra}")
    print("Enter = Daily")

    while True:
        choice = input("Выбор: ").strip() or "1"
        if choice in REPEAT_OPTIONS:
            label, period = REPEAT_OPTIONS[choice]
            if period is not None:
                require_telethon_repeat_support()
            return label, period
        print("Введите номер из списка.")


def build_schedule_times(
    start_minute: int,
    interval_minutes: int,
    posts_count: int,
) -> list[datetime]:
    now = datetime.now(TIMEZONE)
    threshold = now + timedelta(seconds=30)

    base = now.replace(
        hour=START_HOUR,
        minute=start_minute,
        second=0,
        microsecond=0,
    )

    times = []
    for i in range(posts_count):
        dt = base + timedelta(minutes=interval_minutes * i + i)
        if dt <= threshold:
            dt += timedelta(days=1)
        times.append(dt)

    times.sort()
    return times


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
        "Не понял ссылку на пост. Формат должен быть:\n"
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
    print("Куда назначить посты:")
    print()

    for i, dialog in enumerate(dialogs, start=1):
        print(format_dialog_line(i, dialog))

    print()
    print("Можно ввести:")
    print("- номер из списка")
    print("- @username")
    print("- ссылку t.me/...")
    print("- id, например -1001234567890")
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

    source_peer, message_id, link_type = parse_post_link(post_link)

    try:
        source_entity = await client.get_entity(source_peer)
    except Exception as e:
        raise ValueError(
            "Не удалось открыть канал исходного поста. "
            "Аккаунт должен иметь доступ к этому каналу. "
            "Если это private t.me/c ссылка — ты должен быть участником канала."
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


async def copy_scheduled_post(
    client,
    target_entity,
    source_message,
    schedule_time,
    repeat_period: int | None = None,
):
    while True:
        try:
            if repeat_period is None:
                return await client.send_message(
                    target_entity,
                    source_message,
                    schedule=schedule_time,
                )

            peer = await client.get_input_entity(target_entity)
            markup = source_message.reply_markup
            silent = source_message.silent
            has_real_media = (
                source_message.media
                and not isinstance(source_message.media, types.MessageMediaWebPage)
            )

            if has_real_media:
                result = await client(
                    functions.messages.SendMediaRequest(
                        peer=peer,
                        media=utils.get_input_media(source_message.media),
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
    api_id, api_hash = ask_api_credentials()

    client = TelegramClient(SESSION_NAME, api_id, api_hash)

    await client.start()

    me = await client.get_me()
    print(f"\nВошли как: {me.first_name} | id={me.id}")

    source_entity, source_message = await get_source_post(client)

    print()
    print("Исходный пост найден:")
    print(f"Канал: {getattr(source_entity, 'title', None) or getattr(source_entity, 'username', None)}")
    print(f"Message ID: {source_message.id}")

    if source_message.grouped_id:
        print()
        print("Внимание: похоже, это часть альбома/медиагруппы.")
        print("Этот скрипт копирует конкретный пост по ссылке, не весь альбом.")
        print("Repeat Telegram не поддерживает для альбомов.")

    if source_message.entities:
        custom_emoji_count = sum(
            1 for e in source_message.entities
            if e.__class__.__name__ == "MessageEntityCustomEmoji"
        )
        print(f"Entities: {len(source_message.entities)}")
        print(f"Premium/custom emoji entities: {custom_emoji_count}")

    target_entity = await choose_target_chat(client)

    interval_minutes = ask_interval_minutes()
    posts_count = posts_count_for_interval(interval_minutes)
    needed = max(1, math.ceil((COVER_HOURS * 60) / interval_minutes))

    if needed > MAX_SCHEDULED:
        print()
        print(
            f"На {COVER_HOURS}ч нужно было бы {needed} слотов, "
            f"но лимит отложенных ~{MAX_SCHEDULED} — ставлю {posts_count}."
        )

    start_minute = ask_start_minute()
    repeat_label, repeat_period = ask_repeat_period()

    if repeat_period is not None and not getattr(me, "premium", False):
        print()
        print("Внимание: у аккаунта нет Premium-флага — сервер может отклонить repeat.")

    schedule_times = build_schedule_times(
        start_minute=start_minute,
        interval_minutes=interval_minutes,
        posts_count=posts_count,
    )

    print()
    print(
        f"Будет назначено {posts_count} копий "
        f"(каждые ~{interval_minutes} мин):"
    )
    print(f"Repeat: {repeat_label}")
    print()

    for i, dt in enumerate(schedule_times, start=1):
        print(f"{i:>2}. {dt.strftime('%d.%m.%Y %H:%M')}")

    print()
    confirm = input("Запланировать? y/N: ").strip().lower()

    if confirm not in ("y", "yes", "д", "да"):
        print("Отменено.")
        await client.disconnect()
        return

    print()
    print("Ставлю scheduled-копии...")
    print()

    success = 0

    for i, schedule_time in enumerate(schedule_times, start=1):
        try:
            result = await copy_scheduled_post(
                client=client,
                target_entity=target_entity,
                source_message=source_message,
                schedule_time=schedule_time,
                repeat_period=repeat_period,
            )

            success += 1
            scheduled_id = getattr(result, "id", None)
            repeat_info = f" | repeat={repeat_period}s" if repeat_period else ""

            print(
                f"OK {i:>2}/{posts_count} -> "
                f"{schedule_time.strftime('%d.%m.%Y %H:%M')} | "
                f"scheduled id={scheduled_id}{repeat_info}"
            )

            await asyncio.sleep(0.7)

        except RPCError as e:
            print()
            print(f"Ошибка на посте #{i}: {type(e).__name__}: {e}")
            print("Остановлено, чтобы не поставить цепочку частично молча.")
            break

    print()
    print(f"Готово. Успешно запланировано: {success}/{posts_count}")
    if success and repeat_period:
        print("После отправки Telegram сам переставит каждый слот на +period.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
