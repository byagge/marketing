from aiogram import Dispatcher

from app.bot.handlers import (
    acc_chats_router,
    accounts_router,
    facts_router,
    restrictions_router,
    chats_router,
    membership_router,
    menu_router,
    online_router,
    ops_router,
    posts_router,
    sender_router,
    session_maker_router,
    table_router,
)


def setup_routers(dp: Dispatcher) -> None:
    dp.include_router(menu_router)
    dp.include_router(accounts_router)
    dp.include_router(membership_router)
    dp.include_router(acc_chats_router)
    dp.include_router(facts_router)
    dp.include_router(restrictions_router)
    dp.include_router(posts_router)
    dp.include_router(chats_router)
    dp.include_router(table_router)
    dp.include_router(sender_router)
    dp.include_router(online_router)
    dp.include_router(session_maker_router)
    dp.include_router(ops_router)
