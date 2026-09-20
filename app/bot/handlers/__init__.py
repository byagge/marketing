from app.bot.handlers.accounts import router as accounts_router
from app.bot.handlers.chats import router as chats_router
from app.bot.handlers.menu import router as menu_router
from app.bot.handlers.online_settings import router as online_router
from app.bot.handlers.ops import router as ops_router
from app.bot.handlers.posts import router as posts_router
from app.bot.handlers.sender import router as sender_router
from app.bot.handlers.table import router as table_router

__all__ = [
    "accounts_router",
    "chats_router",
    "menu_router",
    "online_router",
    "ops_router",
    "posts_router",
    "sender_router",
    "table_router",
]
