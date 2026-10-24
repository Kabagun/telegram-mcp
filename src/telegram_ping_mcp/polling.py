"""Sequential long polling with a durable acknowledgement boundary."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .telegram import TelegramAPIError, TelegramTransportError

logger = logging.getLogger(__name__)


class OffsetStore(Protocol):
    def get_offset(self) -> int: ...

    def set_offset(self, offset: int) -> None: ...


class UpdateSource(Protocol):
    async def get_updates(self, offset: int) -> list[dict[str, Any]]: ...


class Poller:
    def __init__(
        self,
        bot: UpdateSource,
        store: OffsetStore,
        handle_update: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        retry_base: float = 1.0,
        retry_cap: float = 30.0,
    ) -> None:
        if not 0 < retry_base <= retry_cap:
            raise ValueError("Polling retry delays must be positive and ordered.")
        self._bot = bot
        self._store = store
        self._handle_update = handle_update
        self._retry_base = retry_base
        self._retry_cap = retry_cap

    async def _get_updates(self, offset: int, stop: asyncio.Event) -> list[dict[str, Any]] | None:
        updates_task = asyncio.create_task(self._bot.get_updates(offset))
        stop_task = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                (updates_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                return None
            return await updates_task
        finally:
            for task in (updates_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(updates_task, stop_task, return_exceptions=True)

    @staticmethod
    async def _wait_for_retry(stop: asyncio.Event, delay: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def run(self, stop: asyncio.Event) -> None:
        offset = self._store.get_offset()
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("Persisted Telegram update offset is invalid.")
        retry_delay = self._retry_base
        while not stop.is_set():
            try:
                updates = await self._get_updates(offset, stop)
            except TelegramAPIError as error:
                if error.code != 429 and error.code < 500:
                    raise
                # Server-requested rate-limit waits take precedence over the backoff cap.
                delay = max(retry_delay, error.retry_after or 0)
                await self._wait_for_retry(stop, delay)
                retry_delay = min(retry_delay * 2, self._retry_cap)
                continue
            except TelegramTransportError:
                await self._wait_for_retry(stop, retry_delay)
                retry_delay = min(retry_delay * 2, self._retry_cap)
                continue
            if updates is None:
                return

            handler_failed = False
            for update in updates:
                if stop.is_set():
                    return
                update_id = update["update_id"]
                if update_id < offset:
                    continue
                try:
                    await self._handle_update(update)
                except Exception:
                    # Update content and exception details can contain private messages.
                    logger.warning("Telegram update handling failed; the update will be retried.")
                    handler_failed = True
                    break
                self._store.set_offset(update_id + 1)
                offset = update_id + 1

            if handler_failed:
                await self._wait_for_retry(stop, retry_delay)
                retry_delay = min(retry_delay * 2, self._retry_cap)
            else:
                retry_delay = self._retry_base
