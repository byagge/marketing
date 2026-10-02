"""Надёжная отправка сообщений бота админу.

Раньше любая ошибка отправки глоталась (`except: pass`), а пачка из десятков
сообщений упиралась в flood-лимит Telegram — итоговые отчёты терялись.
Здесь: повтор при RetryAfter, разбиение длинных текстов, склейка пачек
и очередь на чат, чтобы задачи не ждали Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

log = logging.getLogger("marketing.notify")

MAX_LEN = 3900
MERGE_WINDOW = 1.2
SEND_GAP = 1.1


def split_text(text: str, limit: int = MAX_LEN) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


async def safe_send(
    bot: Any,
    chat_id: int,
    text: str,
    *,
    retries: int = 4,
    **kwargs: Any,
) -> bool:
    """Отправить текст; True если все части доставлены."""
    ok = True
    for part in split_text(text):
        sent = False
        for attempt in range(retries):
            try:
                await bot.send_message(chat_id, part, **kwargs)
                sent = True
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(min(float(e.retry_after) + 1.0, 90.0))
            except TelegramBadRequest as e:
                if kwargs.get("parse_mode"):
                    # битая разметка — шлём как обычный текст
                    kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                    continue
                log.warning("send failed (bad request): %s", e)
                break
            except Exception as e:  # сеть и т.п.
                log.warning("send failed (%s), attempt %s", e, attempt + 1)
                await asyncio.sleep(2.0 * (attempt + 1))
        ok = ok and sent
    return ok


class Notifier:
    """Очередь сообщений по чатам: склеивает пачки и не блокирует вызывающего."""

    def __init__(self) -> None:
        self._queues: dict[int, asyncio.Queue[str]] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    def push(self, bot: Any, chat_id: int, text: str) -> None:
        q = self._queues.setdefault(chat_id, asyncio.Queue())
        q.put_nowait(text)
        task = self._tasks.get(chat_id)
        if task is None or task.done():
            self._tasks[chat_id] = asyncio.create_task(self._worker(bot, chat_id, q))

    async def _worker(self, bot: Any, chat_id: int, q: asyncio.Queue[str]) -> None:
        try:
            while True:
                try:
                    first = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    return
                batch = [first]
                await asyncio.sleep(MERGE_WINDOW)
                while not q.empty() and sum(len(x) + 1 for x in batch) < MAX_LEN:
                    batch.append(q.get_nowait())
                merged: list[str] = []
                for item in batch:
                    if not merged or merged[-1] != item:
                        merged.append(item)
                await safe_send(bot, chat_id, "\n".join(merged))
                await asyncio.sleep(SEND_GAP)
        finally:
            if self._tasks.get(chat_id) is asyncio.current_task():
                self._tasks.pop(chat_id, None)

    async def flush(self, timeout: float = 30.0) -> None:
        """Дождаться опустошения очередей (для тестов и остановки)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if all(q.empty() for q in self._queues.values()) and not any(
                t and not t.done() for t in self._tasks.values()
            ):
                return
            await asyncio.sleep(0.2)


notifier = Notifier()
