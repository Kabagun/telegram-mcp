"""Read known bot message IDs through MTProto without storing outgoing bodies.

Only the returned request-local dictionaries contain text. Authentication and
transport state stay in memory; there are no sessions, entity caches, downloads,
history scans, update subscriptions, or outgoing-message writes on disk.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from telethon import TelegramClient, functions, types
from telethon.sessions import MemorySession

_TIMEOUT_SECONDS = 15
_MAX_IDS = 100
_MAX_MESSAGE_ID = 2**31 - 1
_MAX_CHAT_ID = 2**63 - 1
logger = logging.getLogger(__name__)


def _diagnose_failure(operation: str, error: Exception) -> None:
    # Class and source line only. Exception messages/args can contain credentials,
    # request IDs or message bodies and must never enter the application's logs.
    trace = error.__traceback__
    line = 0
    while trace is not None:
        if trace.tb_frame.f_code.co_filename == __file__:
            line = trace.tb_lineno
        trace = trace.tb_next
    logger.warning(
        "Telegram history failed: operation=%s class=%s line=%d.",
        operation,
        type(error).__name__,
        line,
    )


class HistoryUnavailable(Exception):
    """A fixed, content-free error for authorization, response, or transport failures."""

    def __init__(self) -> None:
        super().__init__("Telegram sent-message history is unavailable.")


class _MemoryOnlySession(MemorySession):
    def process_entities(self, tlo: object) -> None:
        """Discard entities, including names and phone numbers in raw responses."""

    def set_update_state(self, entity_id: int, state: object) -> None:
        """No update receiver needs saved update state."""


class _QuietLogger(logging.Logger):
    def getChild(self, suffix: str) -> logging.Logger:
        return self


class _ReadOnlyClient(TelegramClient):
    async def _update_loop(self) -> None:
        # Telethon starts this task even with receive_updates=False. Its normal
        # loop can request differences; this client must never fetch updates.
        return


def _positive_int(value: object, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


def _message_ids(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) > _MAX_IDS:
        raise ValueError("Telegram history message identifiers are invalid.")
    ids = value.copy()
    if any(not _positive_int(item, _MAX_MESSAGE_ID) for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("Telegram history message identifiers are invalid.")
    return ids


class BotHistoryReader:
    """Retrieve at most 100 explicit IDs using a fresh, bot-only memory session.

    The optional client factory has TelegramClient's constructor interface and
    is intended for offline tests. Closing is terminal. Fetches lazily start the
    connection, and all authentication, reads, and shutdowns are serialized.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        bot_token: str,
        *,
        client_factory: Any = None,
    ) -> None:
        if (
            not _positive_int(api_id, _MAX_MESSAGE_ID)
            or not isinstance(api_hash, str)
            or re.fullmatch(r"[a-fA-F0-9]{32}", api_hash) is None
            or not isinstance(bot_token, str)
            or re.fullmatch(r"[1-9][0-9]{0,18}:[A-Za-z0-9_-]{1,256}", bot_token) is None
            or not _positive_int(int(bot_token.partition(":")[0]), _MAX_CHAT_ID)
            or (client_factory is not None and not callable(client_factory))
        ):
            raise ValueError("Telegram history bot credentials are invalid.")
        self._api_id = api_id
        self._api_hash = api_hash
        self._bot_token = bot_token
        self._bot_id = int(bot_token.partition(":")[0])
        self._factory = client_factory if client_factory is not None else _ReadOnlyClient
        self._client: Any = None
        self._ready = False
        self._closed = False
        self._lock = asyncio.Lock()

    async def _disconnect_locked(self) -> None:
        client, self._client = self._client, None
        self._ready = False
        if client is None:
            return

        async def disconnect() -> None:
            try:
                async with asyncio.timeout(_TIMEOUT_SECONDS):
                    await client.disconnect()
            except Exception:
                # Neither transport exceptions nor their request-bearing context
                # may cross the API, including during failed authentication.
                pass

        # Even shutdown cancellation must finish bounded local cleanup. Shield
        # prevents a second cancellation from leaving an authenticated transport
        # running after its reference has been discarded.
        cleanup = asyncio.create_task(disconnect())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def _start_locked(self) -> None:
        if self._closed:
            raise HistoryUnavailable()
        if self._ready:
            return
        failed = False
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                logger = _QuietLogger("telegram_ping_mcp.history.transport")
                logger.disabled = True
                logger.propagate = False
                self._client = self._factory(
                    _MemoryOnlySession(),
                    self._api_id,
                    self._api_hash,
                    receive_updates=False,
                    catch_up=False,
                    timeout=_TIMEOUT_SECONDS,
                    request_retries=0,
                    connection_retries=0,
                    retry_delay=0,
                    auto_reconnect=False,
                    flood_sleep_threshold=0,
                    raise_last_call_error=True,
                    entity_cache_limit=0,
                    base_logger=logger,
                )
                await self._client.connect()
                # sign_in() calls _on_login(), which fetches update differences.
                # Raw bot authorization has no interactive or personal fallback.
                authorization = await self._client(
                    functions.auth.ImportBotAuthorizationRequest(
                        flags=0,
                        api_id=self._api_id,
                        api_hash=self._api_hash,
                        bot_auth_token=self._bot_token,
                    )
                )
                user = getattr(authorization, "user", None)
                if (
                    not isinstance(authorization, types.auth.Authorization)
                    or not isinstance(user, types.User)
                    or user.bot is not True
                    or not _positive_int(user.id, _MAX_CHAT_ID)
                    or user.id != self._bot_id
                ):
                    raise HistoryUnavailable()
                self._ready = True
        except asyncio.CancelledError:
            await self._disconnect_locked()
            raise
        except Exception as error:
            _diagnose_failure("authorize", error)
            failed = True
        if failed:
            await self._disconnect_locked()
            # Raise outside the handler: no secret-bearing exception context.
            raise HistoryUnavailable()

    async def start(self) -> None:
        """Connect and verify the exact bot identity without fetching messages."""
        async with self._lock:
            await self._start_locked()

    async def aclose(self) -> None:
        """Disconnect locally; never log out or revoke the bot's authorization."""
        async with self._lock:
            self._closed = True
            await self._disconnect_locked()

    async def fetch_outgoing(self, chat_id: int, message_ids: list[int]) -> dict[int, dict]:
        """Read only this private chat's bot-sent IDs; fail closed on foreign peers."""
        if not _positive_int(chat_id, _MAX_CHAT_ID):
            raise ValueError("Telegram history private chat identifier is invalid.")
        return await self._fetch(_message_ids(message_ids), chat_id)

    async def fetch_recorded(self, message_ids: list[int]) -> dict[int, dict]:
        """Backfill known successful delivery IDs, omitting non-outgoing/private records.

        This internal path does not grant access to a user. The caller must match
        returned chat IDs against its currently verified users before using them.
        Returned outgoing text must never be persisted.
        """
        return await self._fetch(_message_ids(message_ids), None)

    async def _fetch(self, ids: list[int], chat_id: int | None) -> dict[int, dict]:
        async with self._lock:
            if self._closed:
                raise HistoryUnavailable()
            if not ids:
                return {}
            await self._start_locked()
            failed = False
            try:
                async with asyncio.timeout(_TIMEOUT_SECONDS):
                    response = await self._client(
                        functions.messages.GetMessagesRequest(
                            id=[types.InputMessageID(message_id) for message_id in ids]
                        )
                    )
                    return self._extract(response, set(ids), chat_id)
            except asyncio.CancelledError:
                await self._disconnect_locked()
                raise
            except Exception as error:
                _diagnose_failure("read", error)
                failed = True
            if failed:
                await self._disconnect_locked()
            raise HistoryUnavailable()

    def _extract(self, response: object, requested: set[int], chat_id: int | None) -> dict:
        if not isinstance(response, (types.messages.Messages, types.messages.MessagesSlice)):
            raise HistoryUnavailable()
        messages = response.messages
        if not isinstance(messages, list) or len(messages) > len(requested):
            raise HistoryUnavailable()
        result = {}
        seen = set()
        for message in messages:
            if not isinstance(message, (types.Message, types.MessageEmpty)):
                raise HistoryUnavailable()
            message_id = message.id
            if (
                not _positive_int(message_id, _MAX_MESSAGE_ID)
                or message_id not in requested
                or message_id in seen
            ):
                raise HistoryUnavailable()
            seen.add(message_id)
            if isinstance(message, types.MessageEmpty):
                continue
            peer = message.peer_id
            private = isinstance(peer, types.PeerUser) and _positive_int(peer.user_id, _MAX_CHAT_ID)
            if message.out is not True or not private:
                if chat_id is not None:
                    raise HistoryUnavailable()
                continue
            # Private outgoing messages may omit their implicit self sender.
            # Explicit senders must still identify this authenticated bot.
            sender = message.from_id
            if (
                sender is not None
                and (
                    not isinstance(sender, types.PeerUser)
                    or not _positive_int(sender.user_id, _MAX_CHAT_ID)
                    or sender.user_id != self._bot_id
                )
            ) or (chat_id is not None and peer.user_id != chat_id):
                raise HistoryUnavailable()
            if (
                not isinstance(message.message, str)
                or not isinstance(message.date, datetime)
                or message.date.tzinfo is None
            ):
                raise HistoryUnavailable()
            sent_at = int(message.date.timestamp())
            if not 0 <= sent_at <= _MAX_CHAT_ID:
                raise HistoryUnavailable()
            result[message_id] = {
                "telegram_message_id": message_id,
                "chat_id": peer.user_id,
                "sent_at": sent_at,
                "text": message.message,
                "has_media": message.media is not None
                and not isinstance(message.media, types.MessageMediaEmpty),
            }
        return result
