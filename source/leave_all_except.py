"""
Выходит / удаляет все диалоги, кроме KEEP_CHAT_ID.

Для групп и каналов — LeaveChannel / выход.
Для личных чатов — удаление диалога (история у собеседника не трогается).
"""

import asyncio

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError, UserNotParticipantError
from telethon.tl import functions


SESSION_NAME = "scheduler_session"
KEEP_CHAT_ID = 42777


def ask_api_credentials():
    api_id = 31999582
    api_hash = "d1126aadf79c595b641181fd4d5df2ea"

    if not api_id:
        api_id = input("API_ID: ").strip()

    if not api_hash:
        api_hash = input("API_HASH: ").strip()

    return int(api_id), api_hash


def dialog_ids(dialog) -> set[int]:
    entity = dialog.entity
    ids = {int(entity.id), int(utils.get_peer_id(entity))}
    return ids


def should_keep(dialog) -> bool:
    return KEEP_CHAT_ID in dialog_ids(dialog)


def format_dialog_line(dialog) -> str:
    entity = dialog.entity
    peer_id = utils.get_peer_id(entity)
    username = getattr(entity, "username", None)
    username_text = f"@{username}" if username else "-"

    if dialog.is_user:
        chat_type = "user"
    elif dialog.is_group:
        chat_type = "group"
    elif dialog.is_channel:
        chat_type = "channel"
    else:
        chat_type = "chat"

    return f"{dialog.name} | {chat_type} | id={peer_id} | entity.id={entity.id} | {username_text}"


async def leave_or_delete(client: TelegramClient, dialog) -> None:
    entity = dialog.entity

    # Каналы / супергруппы / megagroup
    if dialog.is_channel:
        try:
            await client(functions.channels.LeaveChannelRequest(entity))
        except UserNotParticipantError:
            pass
        await client.delete_dialog(entity)
        return

    # Обычные группы
    if dialog.is_group:
        try:
            await client(functions.messages.DeleteChatUserRequest(
                chat_id=entity.id,
                user_id="me",
            ))
        except RPCError:
            pass
        await client.delete_dialog(entity)
        return

    # Личные диалоги / боты
    await client.delete_dialog(entity)


async def main():
    api_id, api_hash = ask_api_credentials()
    client = TelegramClient(SESSION_NAME, api_id, api_hash)

    await client.start()
    me = await client.get_me()
    print(f"Аккаунт: {me.first_name} (@{me.username or '-'}) id={me.id}")
    print(f"Оставляем только чат с id={KEEP_CHAT_ID}")
    print()

    to_leave = []
    kept = []

    async for dialog in client.iter_dialogs():
        if should_keep(dialog):
            kept.append(dialog)
            continue
        # Не трогаем «Избранное» (Saved Messages)
        if dialog.is_user and getattr(dialog.entity, "is_self", False):
            kept.append(dialog)
            continue
        to_leave.append(dialog)

    if kept:
        print("Останутся:")
        for d in kept:
            print(f"  KEEP  {format_dialog_line(d)}")
    else:
        print(f"ВНИМАНИЕ: чат {KEEP_CHAT_ID} в диалогах не найден — всё равно удалим остальные.")

    print()
    print(f"К удалению / выходу: {len(to_leave)}")
    for d in to_leave[:30]:
        print(f"  DEL   {format_dialog_line(d)}")
    if len(to_leave) > 30:
        print(f"  ... и ещё {len(to_leave) - 30}")

    if not to_leave:
        print("Нечего удалять.")
        await client.disconnect()
        return

    print()
    confirm = input(f"Удалить / выйти из {len(to_leave)} чатов? напишите YES: ").strip()
    if confirm != "YES":
        print("Отменено.")
        await client.disconnect()
        return

    ok = 0
    fail = 0

    for i, dialog in enumerate(to_leave, 1):
        try:
            await leave_or_delete(client, dialog)
            ok += 1
            print(f"[{i}/{len(to_leave)}] OK  {dialog.name}")
        except FloodWaitError as e:
            print(f"[{i}/{len(to_leave)}] FloodWait {e.seconds}s — жду...")
            await asyncio.sleep(e.seconds + 1)
            try:
                await leave_or_delete(client, dialog)
                ok += 1
                print(f"[{i}/{len(to_leave)}] OK  {dialog.name}")
            except Exception as e2:
                fail += 1
                print(f"[{i}/{len(to_leave)}] FAIL {dialog.name}: {e2}")
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(to_leave)}] FAIL {dialog.name}: {e}")

        await asyncio.sleep(0.35)

    print()
    print(f"Готово. Успешно: {ok}, ошибок: {fail}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
