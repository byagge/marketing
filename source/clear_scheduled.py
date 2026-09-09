import asyncio
from zoneinfo import ZoneInfo

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions


SESSION_NAME = "scheduler_session"
TIMEZONE = ZoneInfo("Asia/Bishkek")

DELETE_BATCH_SIZE = 100
PREVIEW_LIMIT = 20


def ask_api_credentials():
    api_id = 31999582
    api_hash = "d1126aadf79c595b641181fd4d5df2ea"

    if not api_id:
        api_id = input("API_ID: ").strip()

    if not api_hash:
        api_hash = input("API_HASH: ").strip()

    return int(api_id), api_hash


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
    print("Из какого чата удалить отложенные:")
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


def format_schedule_time(message) -> str:
    dt = message.date
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")


def preview_text(message) -> str:
    text = (message.message or "").replace("\n", " ").strip()
    if not text:
        return "(без текста / только медиа)"
    if len(text) > 60:
        return text[:57] + "..."
    return text


def extract_messages(result) -> list:
    if hasattr(result, "messages"):
        return list(result.messages or [])
    return []


async def fetch_scheduled_messages(client: TelegramClient, target_entity):
    while True:
        try:
            result = await client(
                functions.messages.GetScheduledHistoryRequest(
                    peer=target_entity,
                    hash=0,
                )
            )
            return extract_messages(result)

        except FloodWaitError as e:
            print(f"FloodWait: ждём {e.seconds} сек.")
            await asyncio.sleep(e.seconds + 1)

        except RPCError:
            raise


async def delete_scheduled_batch(client, target_entity, message_ids: list[int]):
    while True:
        try:
            return await client(
                functions.messages.DeleteScheduledMessagesRequest(
                    peer=target_entity,
                    id=message_ids,
                )
            )

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

    target_entity = await choose_target_chat(client)

    chat_title = (
        getattr(target_entity, "title", None)
        or getattr(target_entity, "username", None)
        or utils.get_peer_id(target_entity)
    )

    print()
    print(f"Читаю отложенные в: {chat_title}")

    scheduled = await fetch_scheduled_messages(client, target_entity)

    if not scheduled:
        print()
        print("В этом чате нет отложенных сообщений.")
        await client.disconnect()
        return

    scheduled.sort(key=lambda msg: msg.date)

    print()
    print(f"Найдено отложенных: {len(scheduled)}")
    print()

    preview_count = min(len(scheduled), PREVIEW_LIMIT)
    for i, message in enumerate(scheduled[:preview_count], start=1):
        print(
            f"{i:>2}. id={message.id} | "
            f"{format_schedule_time(message)} | "
            f"{preview_text(message)}"
        )

    if len(scheduled) > PREVIEW_LIMIT:
        print(f"... ещё {len(scheduled) - PREVIEW_LIMIT}")

    print()
    confirm = input(
        f"Удалить все {len(scheduled)} отложенных? y/N: "
    ).strip().lower()

    if confirm not in ("y", "yes", "д", "да"):
        print("Отменено.")
        await client.disconnect()
        return

    print()
    print("Удаляю...")

    deleted = 0
    message_ids = [message.id for message in scheduled]

    for start in range(0, len(message_ids), DELETE_BATCH_SIZE):
        batch = message_ids[start:start + DELETE_BATCH_SIZE]

        try:
            await delete_scheduled_batch(client, target_entity, batch)
            deleted += len(batch)
            print(f"OK {deleted}/{len(message_ids)}")
            await asyncio.sleep(0.5)

        except RPCError as e:
            print()
            print(f"Ошибка на пакете: {type(e).__name__}: {e}")
            print("Остановлено.")
            break

    print()
    print(f"Готово. Удалено: {deleted}/{len(scheduled)}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
