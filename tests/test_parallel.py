import asyncio

import pytest

from app.jobs.parallel import map_batches


@pytest.mark.asyncio
async def test_map_batches_parallel_and_order():
    seen: list[int] = []
    lock = asyncio.Lock()

    async def worker(x: int) -> int:
        async with lock:
            seen.append(x)
        await asyncio.sleep(0.01)
        return x * 2

    results = await map_batches(
        [1, 2, 3, 4, 5],
        worker,
        batch_size=2,
        batch_pause=0.0,
    )
    assert results == [2, 4, 6, 8, 10]
    assert set(seen) == {1, 2, 3, 4, 5}
