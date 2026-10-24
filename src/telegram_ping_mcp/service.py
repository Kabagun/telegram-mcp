"""Verified user linking and one-attempt Telegram notifications."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from .onboarding import onboarding_text
from .store import Store
from .telegram import TelegramAPIError, TelegramClient, TelegramTransportError

_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z")
_COMMAND = re.compile(
    r"/(start|help|stop|unlink)(?:@([A-Za-z0-9_]{5,32}))?(?: ([A-Za-z0-9_-]{1,64}))?\Z"
)
_MAX_INTEGER = 2**63 - 1
_FAILED = "Telegram rejected the message."
_UNKNOWN = "Telegram delivery status is unknown."
_INACTIVE = "The user has not enabled notifications."
_INVITATION = (
    "Для подключения откройте персональную ссылку-приглашение, которую вам выдал администратор."
)
_INVALID_LINK = "Ссылка недействительна или устарела. Запросите новое приглашение."
_LINKED = "Связь подтверждена. Уведомления включены."
_STARTED = "Уведомления включены."
_STOPPED = "Уведомления отключены. Чтобы включить их снова, отправьте /start."
_UNLINKED = "Связь с вашим аккаунтом удалена. Для подключения потребуется новое приглашение."
_FORWARDED_FIELDS = {
    "forward_origin",
    "forward_date",
    "forward_from",
    "forward_from_chat",
    "forward_from_message_id",
    "forward_signature",
    "forward_sender_name",
    "is_automatic_forward",
    "sender_chat",
    "via_bot",
    "edit_date",
    "business_connection_id",
    "sender_business_bot",
}


class ServiceError(ValueError):
    """A fixed, content-free error suitable for a public tool response."""


def _safe_text(
    value: object, *, maximum: int, multiline: bool = False, allow_blank: bool = False
) -> str:
    if (
        not isinstance(value, str)
        or (not allow_blank and not value.strip())
        or len(value) > maximum
    ):
        raise ServiceError("Text is missing or exceeds the allowed length.")
    for character in value:
        category = unicodedata.category(character)
        if category == "Cs" or (category == "Cc" and not (multiline and character in "\n\t")):
            raise ServiceError("Text contains unsupported characters.")
    return value


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _message_text(value: object) -> str:
    result = _safe_text(value, maximum=4096, multiline=True)
    if _utf16_length(result) > 4096:
        raise ServiceError("Message exceeds the allowed length.")
    return result


def _integer(value: object, *, minimum: int = 1, maximum: int = _MAX_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ServiceError("Numeric identifier or limit is invalid.")
    return value


class Service:
    def __init__(
        self,
        store: Store,
        bot: TelegramClient,
        bot_username: str,
        *,
        link_ttl_seconds: int = 900,
        allowed_group_chat_ids: frozenset[int] = frozenset(),
        public_mcp_url: str = "",
    ) -> None:
        if not isinstance(bot_username, str):
            raise ServiceError("Bot username is invalid.")
        bot_username = bot_username.removeprefix("@")
        if _USERNAME.fullmatch(bot_username) is None:
            raise ServiceError("Bot username is invalid.")
        _integer(link_ttl_seconds, minimum=60, maximum=86400)
        if not isinstance(allowed_group_chat_ids, frozenset):
            raise ServiceError("Group allowlist is invalid.")
        for chat_id in allowed_group_chat_ids:
            _integer(chat_id, minimum=-_MAX_INTEGER, maximum=-1)
        self._store = store
        self._bot = bot
        self._bot_username = bot_username
        self._link_ttl_seconds = link_ttl_seconds
        self._allowed_group_chat_ids = allowed_group_chat_ids
        self._onboarding_text = onboarding_text(public_mcp_url)

    @property
    def store(self) -> Store:
        return self._store

    def create_user_link(
        self, user_key: str, display_name: str, ttl_seconds: int | None = None
    ) -> dict[str, Any]:
        user_key = _safe_text(user_key, maximum=128)
        display_name = _safe_text(display_name, maximum=128)
        lifetime = self._link_ttl_seconds if ttl_seconds is None else ttl_seconds
        _integer(lifetime, minimum=60, maximum=86400)
        link = self._store.create_link(user_key, display_name, lifetime)
        return {
            "link": f"https://t.me/{self._bot_username}?start={link['token']}",
            "expires_at": link["expires_at"],
            "user_key": user_key,
        }

    def find_users(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        query = _safe_text(query, maximum=256, allow_blank=True)
        _integer(limit, minimum=1, maximum=100)
        return self._store.list_users(query, limit)

    def get_user(self, user_key: str) -> dict[str, Any] | None:
        return self._store.get_user(_safe_text(user_key, maximum=128))

    def unlink_user(self, user_key: str) -> dict[str, Any]:
        user_key = _safe_text(user_key, maximum=128)
        return {"user_key": user_key, "unlinked": self._store.delete_user(user_key)}

    def _binding(self, user_key: str) -> dict[str, Any]:
        user = self._store.get_user(_safe_text(user_key, maximum=128))
        if user is None:
            raise ServiceError("User has no verified Telegram binding.")
        telegram_id = _integer(user.get("telegram_user_id"))
        if _integer(user.get("chat_id")) != telegram_id:
            raise ServiceError("User has no verified Telegram binding.")
        return user

    @staticmethod
    def _fingerprint(
        kind: str,
        target: int,
        users: Sequence[dict[str, Any]],
        text: str,
        topic: int | None,
        reply_to_message_id: int | None = None,
    ) -> str:
        payload = {
            "type": kind,
            "target": target,
            "user_keys": [user["user_key"] for user in users],
            "telegram_user_ids": [user["telegram_user_id"] for user in users],
            "text": text,
            "topic": topic,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _result(request_id: str, delivery: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
        result = {
            "status": delivery["status"],
            "message_id": delivery.get("message_id"),
            "request_id": request_id,
            "duplicate": duplicate,
        }
        if "error" in delivery:
            result["error"] = delivery["error"]
        return result

    async def _deliver(
        self,
        *,
        kind: str,
        chat_id: int,
        users: Sequence[dict[str, Any]],
        text: str,
        request_id: str,
        topic_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        request_id = _safe_text(request_id, maximum=128)
        fingerprint = self._fingerprint(kind, chat_id, users, text, topic_id, reply_to_message_id)
        try:
            delivery = self._store.reserve_delivery(request_id, fingerprint)
        except ValueError:
            raise ServiceError("Request ID was already used for a different delivery.") from None
        if not delivery["is_new"]:
            return self._result(request_id, delivery, duplicate=True)
        if any(user.get("active") is not True for user in users):
            self._store.finish_delivery(request_id, "failed", error=_INACTIVE)
            raise ServiceError(_INACTIVE)
        try:
            reply_options = (
                {"reply_to_message_id": reply_to_message_id}
                if reply_to_message_id is not None
                else {}
            )
            message = await self._bot.send_message(
                chat_id, text, entities=entities, message_thread_id=topic_id, **reply_options
            )
        except TelegramAPIError as error:
            self._store.finish_delivery(request_id, "failed", error=_FAILED)
            if chat_id > 0 and error.code == 403:
                self._store.set_active_by_telegram(users[0]["telegram_user_id"], False)
            return self._result(request_id, {"status": "failed", "error": _FAILED}, duplicate=False)
        except TelegramTransportError:
            self._store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
            return self._result(
                request_id, {"status": "unknown", "error": _UNKNOWN}, duplicate=False
            )
        except asyncio.CancelledError:
            self._store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
            raise
        except Exception:
            self._store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
            raise ServiceError(_UNKNOWN) from None
        if (
            not isinstance(message, dict)
            or isinstance(message.get("message_id"), bool)
            or not isinstance(message.get("message_id"), int)
            or not 1 <= message["message_id"] <= _MAX_INTEGER
        ):
            self._store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
            return self._result(
                request_id, {"status": "unknown", "error": _UNKNOWN}, duplicate=False
            )
        self._store.finish_delivery(request_id, "sent", message_id=message["message_id"])
        return self._result(
            request_id, {"status": "sent", "message_id": message["message_id"]}, duplicate=False
        )

    async def ping_user(self, user_key: str, text: str, request_id: str) -> dict[str, Any]:
        user = self._binding(user_key)
        return await self._deliver(
            kind="user",
            chat_id=user["chat_id"],
            users=[user],
            text=_message_text(text),
            request_id=request_id,
        )

    async def reply_message(
        self, message: dict[str, Any], text: str, request_id: str
    ) -> dict[str, Any]:
        """Reply to a trusted stored inbox row, retaining its chat and forum topic."""
        user = self._binding(message["user_key"])
        if user["telegram_user_id"] != message["telegram_user_id"]:
            raise ServiceError("The original sender is no longer linked to this user.")
        chat_id = _integer(message["chat_id"], minimum=-_MAX_INTEGER, maximum=_MAX_INTEGER)
        if chat_id != user["chat_id"] and chat_id not in self._allowed_group_chat_ids:
            raise ServiceError("The original chat is no longer allowed.")
        reply_id = _integer(message["telegram_message_id"])
        topic_id = message.get("topic_id")
        if topic_id is not None:
            _integer(topic_id)
        return await self._deliver(
            kind="reply",
            chat_id=chat_id,
            users=[user],
            text=_message_text(text),
            request_id=request_id,
            topic_id=topic_id,
            reply_to_message_id=reply_id,
        )

    async def ping_group(
        self,
        chat_id: int,
        user_keys: list[str],
        text: str,
        request_id: str,
        topic_id: int | None = None,
    ) -> dict[str, Any]:
        chat_id = _integer(chat_id, minimum=-_MAX_INTEGER, maximum=-1)
        if chat_id not in self._allowed_group_chat_ids:
            raise ServiceError("Group is not allowed.")
        if topic_id is not None:
            _integer(topic_id)
        if not isinstance(user_keys, (list, tuple)) or not 1 <= len(user_keys) <= 50:
            raise ServiceError("Specify between one and fifty unique users.")
        keys = [_safe_text(key, maximum=128) for key in user_keys]
        if len(set(keys)) != len(keys):
            raise ServiceError("Specify between one and fifty unique users.")
        users = [self._binding(key) for key in keys]
        composed = _message_text(text) + "\n\n"
        entities = []
        for index, user in enumerate(users):
            name = _safe_text(user["display_name"], maximum=128)
            if index:
                composed += ", "
            entities.append(
                {
                    "type": "text_mention",
                    "offset": _utf16_length(composed),
                    "length": _utf16_length(name),
                    "user": {
                        "id": user["telegram_user_id"],
                        "is_bot": False,
                        "first_name": name,
                    },
                }
            )
            composed += name
        _message_text(composed)
        return await self._deliver(
            kind="group",
            chat_id=chat_id,
            users=users,
            text=composed,
            request_id=request_id,
            topic_id=topic_id,
            entities=entities,
        )

    async def _reply(
        self, telegram_id: int, text: str, *, deactivate_on_forbidden: bool = True
    ) -> None:
        try:
            await self._bot.send_message(telegram_id, text)
        except TelegramAPIError as error:
            if error.code == 403 and deactivate_on_forbidden:
                self._store.set_active_by_telegram(telegram_id, False)
        except TelegramTransportError:
            # The command's durable state is already applied. Never retry the reply.
            pass

    async def handle_update(self, update: dict[str, Any]) -> None:
        if not isinstance(update, dict) or not isinstance(update.get("message"), dict):
            return
        message = update["message"]
        if any(field in message for field in _FORWARDED_FIELDS):
            return
        chat = message.get("chat")
        author = message.get("from")
        if not isinstance(chat, dict) or not isinstance(author, dict):
            return
        if chat.get("type") != "private" or author.get("is_bot") is not False:
            return
        try:
            telegram_id = _integer(author.get("id"))
            if _integer(chat.get("id")) != telegram_id:
                return
        except ServiceError:
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        command = _COMMAND.fullmatch(text)
        if command is None:
            return
        name, username, token = command.groups()
        if username is not None and username.casefold() != self._bot_username.casefold():
            return
        if token is not None and name != "start":
            return
        if name == "help":
            await self._reply(telegram_id, self._onboarding_text, deactivate_on_forbidden=False)
            return
        if name == "start" and token is not None:
            username = author.get("username")
            first_name = author.get("first_name", "")
            if (username is not None and not isinstance(username, str)) or not isinstance(
                first_name, str
            ):
                return
            try:
                self._store.consume_link(token, telegram_id, telegram_id, username, first_name)
            except ValueError:
                response = _INVALID_LINK
            else:
                response = _LINKED
        elif name == "start":
            if self._store.set_active_by_telegram(telegram_id, True):
                response = _STARTED
            else:
                response = _INVITATION
        elif name == "stop":
            self._store.set_active_by_telegram(telegram_id, False)
            response = _STOPPED
        else:
            user = self._store.get_user_by_telegram(telegram_id)
            if user is not None:
                self._store.delete_user(user["user_key"])
            response = _UNLINKED
        if name == "start":
            response += "\n\n" + self._onboarding_text
        await self._reply(telegram_id, response)
