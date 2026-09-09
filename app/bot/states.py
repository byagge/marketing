from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    session = State()
    label = State()


class AccountFile(StatesGroup):
    telethon = State()
    pyrogram = State()
    bot_token = State()
    sender_id = State()
    rename = State()


class EditPost(StatesGroup):
    ru = State()
    en = State()
    cloak = State()


class EditChat(StatesGroup):
    add = State()
    tag = State()
    interval = State()
    title = State()


class EditTable(StatesGroup):
    minute = State()
    upload = State()


class EditSettings(StatesGroup):
    between = State()
    cycle = State()
    per_chat = State()
    parallel = State()
    keep_ids = State()


class LeaveFSM(StatesGroup):
    confirm = State()
