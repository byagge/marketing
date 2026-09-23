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
    invite = State()
    garant_bot = State()
    after_bot = State()
    require_channels = State()


class FolderJoin(StatesGroup):
    link = State()


class EditTable(StatesGroup):
    minute = State()
    upload = State()


class EditSettings(StatesGroup):
    between = State()
    cycle = State()
    per_chat = State()
    parallel = State()
    keep_ids = State()
    online_hours = State()


class LeaveFSM(StatesGroup):
    confirm = State()


class MakeSession(StatesGroup):
    kind = State()
    name = State()
    phone = State()
    code = State()
    password = State()
