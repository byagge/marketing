from __future__ import annotations

from aiogram import Bot

from app.store import Store


class Ctx:
    store: Store
    bot: Bot | None = None


ctx = Ctx()
