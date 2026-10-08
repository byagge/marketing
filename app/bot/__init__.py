from aiogram import Dispatcher

from app.bot.handlers import (
    accounts_router,
    autopilot_router,
    chats_router,
    membership_router,
    menu_router,
    online_router,
    ops_router,
    posts_router,
    prefs_router,
    redesign_router,
    sender_router,
    session_maker_router,
    table_router,
)


def setup_routers(dp: Dispatcher) -> None:
    dp.include_router(menu_router)
    dp.include_router(accounts_router)
    dp.include_router(prefs_router)
    dp.include_router(autopilot_router)
    dp.include_router(redesign_router)
    dp.include_router(membership_router)
    dp.include_router(posts_router)
    dp.include_router(chats_router)
    dp.include_router(table_router)
    dp.include_router(sender_router)
    dp.include_router(online_router)
    dp.include_router(session_maker_router)
    dp.include_router(ops_router)
