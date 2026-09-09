from aiogram import Dispatcher

from app.bot.handlers import (
    accounts_router,
    chats_router,
    menu_router,
    ops_router,
    posts_router,
    sender_router,
    table_router,
)


def setup_routers(dp: Dispatcher) -> None:
    dp.include_router(menu_router)
    dp.include_router(accounts_router)
    dp.include_router(posts_router)
    dp.include_router(chats_router)
    dp.include_router(table_router)
    dp.include_router(sender_router)
    dp.include_router(ops_router)
