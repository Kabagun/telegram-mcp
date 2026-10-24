import asyncio
from collections import deque

import pytest

from telegram_ping_mcp.polling import Poller
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramTransportError


class MemoryOffsetStore:
    def __init__(self, offset=0):
        self.offset = offset
        self.saved = []

    def get_offset(self):
        return self.offset

    def set_offset(self, offset):
        self.offset = offset
        self.saved.append(offset)


class ScriptedBot:
    def __init__(self, *results):
        self.results = deque(results)
        self.offsets = []
        self.blocked = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def get_updates(self, offset):
        self.offsets.append(offset)
        if not self.results:
            self.blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


async def test_offsets_persist_after_each_successful_handler_and_stale_updates_are_skipped():
    store = MemoryOffsetStore(40)
    bot = ScriptedBot([{"update_id": 39}, {"update_id": 40}, {"update_id": 41}])
    handled = []
    stop = asyncio.Event()

    async def handle(update):
        handled.append(update["update_id"])
        assert store.offset == update["update_id"]

    task = asyncio.create_task(Poller(bot, store, handle).run(stop))
    await asyncio.wait_for(bot.blocked.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert handled == [40, 41]
    assert store.saved == [41, 42]
    assert bot.offsets == [40, 42]
    assert bot.cancelled.is_set()


async def test_handler_failure_retries_current_offset_before_later_updates(caplog):
    store = MemoryOffsetStore(10)
    batch = [{"update_id": 10}, {"update_id": 11}, {"update_id": 12}]
    bot = ScriptedBot(batch, batch[1:])
    handled = []
    fail_once = True
    stop = asyncio.Event()

    async def handle(update):
        nonlocal fail_once
        handled.append(update["update_id"])
        if update["update_id"] == 11 and fail_once:
            fail_once = False
            raise RuntimeError("synthetic-private-content-and-token")

    task = asyncio.create_task(
        Poller(bot, store, handle, retry_base=0.001, retry_cap=0.01).run(stop)
    )
    await asyncio.wait_for(bot.blocked.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert handled == [10, 11, 11, 12]
    assert store.saved == [11, 12, 13]
    assert bot.offsets == [10, 11, 13]
    assert "synthetic-private-content-and-token" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("code", [400, 401, 403, 409])
async def test_permanent_errors_fail_without_retrying(code):
    store = MemoryOffsetStore(7)
    bot = ScriptedBot(TelegramAPIError(code))

    async def handle(update):
        pytest.fail("No update should be handled")

    with pytest.raises(TelegramAPIError) as caught:
        await Poller(bot, store, handle).run(asyncio.Event())
    assert caught.value.code == code
    assert bot.offsets == [7]
    assert store.saved == []


async def test_transient_retry_backoff_is_capped_and_rate_limit_delay_is_honored():
    store = MemoryOffsetStore(7)
    bot = ScriptedBot(
        TelegramTransportError(),
        TelegramAPIError(500),
        TelegramTransportError(),
        TelegramAPIError(429, retry_after=9),
    )
    stop = asyncio.Event()
    waits = []

    async def handle(update):
        pytest.fail("No update should be handled")

    poller = Poller(bot, store, handle, retry_base=1, retry_cap=2)

    async def wait_for_retry(event, delay):
        waits.append(delay)
        if len(waits) == 4:
            event.set()

    poller._wait_for_retry = wait_for_retry
    await poller.run(stop)
    assert waits == [1, 2, 2, 9]
    assert bot.offsets == [7, 7, 7, 7]
    assert store.saved == []


async def test_stop_interrupts_long_backoff():
    bot = ScriptedBot(TelegramTransportError())
    stop = asyncio.Event()
    started_waiting = asyncio.Event()

    async def handle(update):
        pytest.fail("No update should be handled")

    poller = Poller(bot, MemoryOffsetStore(), handle, retry_base=60, retry_cap=60)
    real_wait = poller._wait_for_retry

    async def wait_for_retry(event, delay):
        started_waiting.set()
        await real_wait(event, delay)

    poller._wait_for_retry = wait_for_retry
    task = asyncio.create_task(poller.run(stop))
    await asyncio.wait_for(started_waiting.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert bot.offsets == [0]


async def test_cancellation_interrupts_long_poll_and_cleans_up_child_tasks():
    bot = ScriptedBot()
    store = MemoryOffsetStore()

    async def handle(update):
        pytest.fail("No update should be handled")

    task = asyncio.create_task(Poller(bot, store, handle).run(asyncio.Event()))
    await asyncio.wait_for(bot.blocked.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bot.cancelled.is_set()
    assert store.saved == []


async def test_cancellation_during_handler_does_not_ack_update():
    bot = ScriptedBot([{"update_id": 5}, {"update_id": 6}])
    store = MemoryOffsetStore(5)
    handling = asyncio.Event()
    handled = []

    async def handle(update):
        handled.append(update["update_id"])
        handling.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(Poller(bot, store, handle).run(asyncio.Event()))
    await asyncio.wait_for(handling.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handled == [5]
    assert store.saved == []


async def test_store_failure_does_not_advance_offset_or_handle_next_update():
    class FailingStore(MemoryOffsetStore):
        def set_offset(self, offset):
            raise OSError("synthetic storage failure")

    bot = ScriptedBot([{"update_id": 5}, {"update_id": 6}])
    store = FailingStore(5)
    handled = []

    async def handle(update):
        handled.append(update["update_id"])

    with pytest.raises(OSError):
        await Poller(bot, store, handle).run(asyncio.Event())
    assert handled == [5]
    assert store.offset == 5


async def test_already_stopped_poller_does_not_make_requests():
    bot = ScriptedBot()
    stop = asyncio.Event()
    stop.set()

    async def handle(update):
        pytest.fail("No update should be handled")

    await Poller(bot, MemoryOffsetStore(), handle).run(stop)
    assert bot.offsets == []
