import asyncio

from telethon.tl import functions

from app.tg.online import bump_online


def test_bump_online_calls_update_status():
    calls: list[bool] = []

    class FakeClient:
        async def __call__(self, req):
            assert isinstance(req, functions.account.UpdateStatusRequest)
            calls.append(bool(req.offline))

    asyncio.run(bump_online(FakeClient(), hold_seconds=0.01))
    # First online (offline=False), then offline (offline=True)
    assert calls == [False, True]
