"""Параллельный запуск задач пачками (анти-Flood между пачками)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from app.config import get_settings

T = TypeVar("T")
R = TypeVar("R")


def setup_parallel_defaults() -> tuple[int, float]:
    s = get_settings()
    return max(1, int(s.setup_parallel)), max(0.0, float(s.setup_batch_pause_sec))


async def map_batches(
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    batch_size: int | None = None,
    batch_pause: float | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_batch: Callable[[int, Sequence[T]], Awaitable[None] | None] | None = None,
) -> list[R | BaseException]:
    """
    Обработать items пачками: внутри пачки — asyncio.gather,
    между пачками — короткая пауза.
    """
    default_size, default_pause = setup_parallel_defaults()
    size = max(1, int(batch_size if batch_size is not None else default_size))
    pause = float(batch_pause if batch_pause is not None else default_pause)
    out: list[R | BaseException] = []
    total = len(items)
    for i in range(0, total, size):
        if is_cancelled and is_cancelled():
            break
        batch = items[i : i + size]
        if on_batch is not None:
            maybe = on_batch(i // size + 1, batch)
            if asyncio.iscoroutine(maybe):
                await maybe
        results = await asyncio.gather(
            *[worker(item) for item in batch],
            return_exceptions=True,
        )
        out.extend(results)
        if i + size < total and pause > 0:
            await asyncio.sleep(pause)
    return out
