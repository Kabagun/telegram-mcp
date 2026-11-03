"""Merge stored incoming messages with outgoing bodies fetched from Telegram."""

from __future__ import annotations

import asyncio
from typing import Any

from .inbox import InboxStore
from .service import ServiceError, _integer
from .store import Store
from .telegram_history import BotHistoryReader


class ConversationHistory:
    """Keep outgoing bodies in memory, scoped to a currently verified private chat."""

    def __init__(
        self, store: Store, inbox: InboxStore, reader: BotHistoryReader | None = None
    ) -> None:
        self.store = store
        self.inbox = inbox
        self.reader = reader
        self._backfill_lock = asyncio.Lock()

    def _require_binding(self, user: dict[str, Any]) -> None:
        current = self.store.get_user(user["user_key"])
        if (
            current is None
            or current.get("active") is not True
            or current.get("telegram_user_id") != user["telegram_user_id"]
            or current.get("chat_id") != user["telegram_user_id"]
        ):
            raise ServiceError("Access denied.")

    async def _backfill(self) -> None:
        """Import at most 100 previously acknowledged IDs, never arbitrary history."""
        if self.reader is None:
            return
        async with self._backfill_lock:
            ids = self.store.list_sent_delivery_ids(self.store.get_history_backfill_cursor())
            if not ids:
                return
            messages = await self.reader.fetch_recorded(ids)
            for message in messages.values():
                user = self.store.get_user_by_telegram(message["chat_id"])
                if (
                    user is not None
                    and user.get("active") is True
                    and user.get("chat_id") == user["telegram_user_id"]
                ):
                    # Deliberately project metadata: no body enters the index.
                    self.inbox.record_outgoing(
                        user,
                        {
                            "message_id": message["telegram_message_id"],
                            "chat": {"id": message["chat_id"], "type": "private"},
                            "date": message["sent_at"],
                        },
                    )
            self.store.set_history_backfill_cursor(ids[-1])

    async def get_messages(
        self,
        user: dict[str, Any],
        *,
        message_id: int | None = None,
        after_id: int = 0,
        limit: int = 50,
        direction: str = "all",
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        if direction not in {"all", "incoming", "outgoing"}:
            raise ServiceError("Direction must be all, incoming, or outgoing.")
        _integer(after_id, minimum=0)
        _integer(limit, maximum=100)
        if message_id is not None:
            _integer(message_id)
        self._require_binding(user)
        if direction != "incoming":
            await self._backfill()
            self._require_binding(user)
        if message_id is not None:
            item = self.inbox.get_message(message_id, include_direction=True)
            items = [] if item is None else [item]
        else:
            items = self.inbox.list_messages(
                user["user_key"],
                user["telegram_user_id"],
                after_id,
                limit,
                direction=direction,
                include_direction=True,
            )
        for item in items:
            if (
                item["user_key"] != user["user_key"]
                or item["telegram_user_id"] != user["telegram_user_id"]
                or item["chat_id"] != user["telegram_user_id"]
                or item["topic_id"] is not None
            ):
                raise ServiceError("Access denied.")
        if message_id is not None:
            items = [item for item in items if direction in {"all", item["direction"]}]
        outgoing = [item for item in items if item["direction"] == "outgoing"]
        if outgoing and self.reader is not None:
            bodies = await self.reader.fetch_outgoing(
                user["telegram_user_id"], [item["telegram_message_id"] for item in outgoing]
            )
            self._require_binding(user)
            for item in outgoing:
                body = bodies.get(item["telegram_message_id"])
                if body is not None:
                    field = "caption" if body["has_media"] else "text"
                    item[field] = body["text"]
                    item["content_available"] = True
        self._require_binding(user)
        return (items[0] if items else None) if message_id is not None else items
