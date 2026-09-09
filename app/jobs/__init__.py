from __future__ import annotations

import asyncio
from typing import Any

from app.store import Store


class JobRuntime:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel: dict[str, bool] = {}

    def key(self, kind: str, account_id: int) -> str:
        return f"{kind}:{account_id}"

    def is_running(self, kind: str, account_id: int) -> bool:
        task = self._tasks.get(self.key(kind, account_id))
        return bool(task and not task.done())

    def running_keys(self) -> list[str]:
        return [k for k, t in self._tasks.items() if t and not t.done()]

    def request_cancel(self, kind: str, account_id: int) -> None:
        self._cancel[self.key(kind, account_id)] = True

    def cancelled(self, kind: str, account_id: int) -> bool:
        return bool(self._cancel.get(self.key(kind, account_id)))

    def spawn(self, kind: str, account_id: int, coro, on_done=None) -> asyncio.Task:
        key = self.key(kind, account_id)
        if self.is_running(kind, account_id):
            raise RuntimeError("Уже запущено")
        self._cancel[key] = False

        async def _runner():
            try:
                await coro
            finally:
                self._tasks.pop(key, None)
                if on_done is not None:
                    try:
                        result = on_done()
                        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                            await result
                    except Exception:
                        pass

        task = asyncio.create_task(_runner(), name=key)
        self._tasks[key] = task
        return task


runtime = JobRuntime()


class LogSink:
    def __init__(self, store: Store, job_id: int, bot=None, chat_id: int | None = None) -> None:
        self.store = store
        self.job_id = job_id
        self.bot = bot
        self.chat_id = chat_id

    async def emit(self, message: str, level: str = "info", notify: bool = True) -> None:
        await self.store.add_log(self.job_id, message, level)
        if notify and self.bot and self.chat_id:
            try:
                await self.bot.send_message(self.chat_id, message)
            except Exception:
                pass


def parse_keep_ids(raw: str, chat_ids: list[str]) -> set[int]:
    ids: set[int] = set()
    for item in chat_ids:
        item = str(item).strip()
        if item.lstrip("-").isdigit():
            ids.add(int(item))
            ids.add(abs(int(item)))
            if item.startswith("-100"):
                ids.add(int(item[4:]))
    for part in (raw or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids
