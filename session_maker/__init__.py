"""
Создание Telethon / Pyrogram .session.

CLI:
  python -m session_maker

В боте: меню → Session (FSM в app.bot.handlers.session_maker).
"""

from __future__ import annotations

__all__ = ["run_cli"]

from session_maker.cli import run_cli
