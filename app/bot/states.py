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
    ru_short = State()
    en_short = State()
    # ссылки на сообщения канала
    link_ru = State()
    link_en = State()
    link_ru_short = State()
    link_en_short = State()
    link_ru_photo = State()
    link_en_photo = State()
    link_ru_short_photo = State()
    link_en_short_photo = State()
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
