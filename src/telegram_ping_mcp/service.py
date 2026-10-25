"""Verified user linking and one-attempt Telegram notifications."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Sequence
from time import monotonic
from typing import Any

from .onboarding import (
    BUTTON_ACTIONS,
    CALLBACK_ACTIONS,
    client_picker,
    command_menu,
    help_presentation,
    intro_text,
    menu_keyboard,
    onboarding_presentation,
    onboarding_text,
    privacy_presentation,
    unlink_presentation,
)
from .store import Store
from .telegram import TelegramAPIError, TelegramClient, TelegramTransportError

_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z")
_COMMAND = re.compile(
    r"/(start|connect|unlink)(?:@([A-Za-z0-9_]{5,32}))?(?: ([A-Za-z0-9_-]{1,64}))?\Z"
)
_RETIRED_COMMAND = re.compile(r"/stop(?:@([A-Za-z0-9_]{5,32}))?\Z")
_RETIRED_BUTTONS = frozenset({"⏸ Пауза", "🔔 Включить", "ℹ️ Помощь"})
_RETIRED_CALLBACKS = frozenset({"ui:stop", "ui:start", "ui:help", "ui:menu"})
_MAX_INTEGER = 2**63 - 1
_FAILED = "Telegram rejected the message."
_UNKNOWN = "Telegram delivery status is unknown."
_INACTIVE = "Telegram delivery is unavailable for this user."
_INVALID_LINK = "Ссылка недействительна или устарела. Запросите новое приглашение."
_LINKED = "Связь подтверждена."
_UNLINKED = "Все ваши AI-приложения отключены. Подключиться заново можно кнопкой ниже."
_UNLINK_RETRY = "Не удалось удалить привязку. Попробуйте ещё раз."
_CONNECT = "Нажмите «🔌 Подключить AI-приложение», чтобы начать подключение."
_CONNECT_RETRY = "Не удалось получить код входа. Нажмите «🔌 Подключить AI-приложение» ещё раз."
_CONNECT_ICON_NOTE = "📎 Иконка PNG — файлом ниже."
_CONNECT_ICON_RETRY = (
    "Не удалось прикрепить иконку. Скачайте PNG кнопкой в инструкции или повторите подключение."
)
logger = logging.getLogger(__name__)
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
        login_code_issuer: Callable[[str], str] | None = None,
        user_unlinked: Callable[[str], None] | None = None,
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
        self._public_mcp_url = public_mcp_url
        onboarding_text(public_mcp_url)
        self._login_code_issuer = login_code_issuer
        self._user_unlinked = user_unlinked
        self._pending_unlinks: dict[int, tuple[str, float]] = {}

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
        if self._user_unlinked is not None and self._store.get_user(user_key) is not None:
            try:
                self._user_unlinked(user_key)
            except Exception:
                raise ServiceError("Could not unlink the user. Try again.") from None
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
        self,
        telegram_id: int,
        text: str,
        *,
        entities: list[dict[str, Any]] | None = None,
        reply_markup: dict[str, Any] | None = None,
        deactivate_on_forbidden: bool = True,
    ) -> bool:
        options: dict[str, Any] = {}
        if entities is not None:
            options["entities"] = entities
        if reply_markup is not None:
            options["reply_markup"] = reply_markup
        try:
            await self._bot.send_message(telegram_id, text, **options)
        except TelegramAPIError as error:
            if error.code == 403 and deactivate_on_forbidden:
                self._store.set_active_by_telegram(telegram_id, False)
        except TelegramTransportError:
            # The command's durable state is already applied. Never retry the reply.
            pass
        else:
            return True
        return False

    async def _refresh_commands(self, telegram_id: int) -> None:
        linked = self._store.get_user_by_telegram(telegram_id) is not None
        try:
            await self._bot.set_commands(command_menu(linked=linked), chat_id=telegram_id)
        except (TelegramAPIError, TelegramTransportError):
            logger.warning("Telegram chat command menu could not be updated.")

    def _keyboard(self, telegram_id: int) -> dict[str, Any]:
        user = self._store.get_user_by_telegram(telegram_id)
        return menu_keyboard(linked=user is not None)

    @staticmethod
    def _private_author(message: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        if any(field in message for field in _FORWARDED_FIELDS):
            return None
        chat = message.get("chat")
        author = message.get("from")
        if not isinstance(chat, dict) or not isinstance(author, dict):
            return None
        if chat.get("type") != "private" or author.get("is_bot") is not False:
            return None
        try:
            telegram_id = _integer(author.get("id"))
            if _integer(chat.get("id")) != telegram_id:
                return None
            Service._author_profile(author)
        except ServiceError:
            return None
        return telegram_id, author

    @staticmethod
    def _author_profile(author: dict[str, Any]) -> tuple[str | None, str]:
        username = author.get("username")
        if username is not None:
            username = _safe_text(username, maximum=128, allow_blank=True)
        first_name = _safe_text(author.get("first_name", ""), maximum=128, allow_blank=True)
        return username, first_name

    def _callback_control(self, query: object) -> tuple[str, int, dict[str, Any], str] | None:
        if not isinstance(query, dict) or "inline_message_id" in query:
            return None
        query_id = query.get("id")
        action = query.get("data")
        if (
            not isinstance(query_id, str)
            or not 1 <= len(query_id) <= 256
            or any(not "!" <= character <= "~" for character in query_id)
            or not isinstance(action, str)
            or action not in CALLBACK_ACTIONS | _RETIRED_CALLBACKS
        ):
            return None
        message = query.get("message")
        if not isinstance(message, dict) or any(field in message for field in _FORWARDED_FIELDS):
            return None
        bot_author = message.get("from")
        if (
            not isinstance(bot_author, dict)
            or bot_author.get("is_bot") is not True
            or not isinstance(bot_author.get("username"), str)
            or bot_author["username"].casefold() != self._bot_username.casefold()
        ):
            return None
        try:
            _integer(bot_author.get("id"))
            _integer(message.get("message_id"))
            _integer(message.get("date"))
        except ServiceError:
            return None
        private = self._private_author({"chat": message.get("chat"), "from": query.get("from")})
        if private is None:
            return None
        telegram_id, author = private
        return action, telegram_id, author, query_id

    def _connect_text(self, telegram_id: int, *, client: str = "chatgpt") -> str:
        user = self._store.get_user_by_telegram(telegram_id)
        if user is None:
            return onboarding_text(self._public_mcp_url, client=client)
        if user.get("active") is not True:
            return _CONNECT_RETRY
        response = onboarding_text(self._public_mcp_url, linked=True, client=client)
        if self._login_code_issuer is None:
            return response
        try:
            binding = self._binding(user["user_key"])
            if binding["telegram_user_id"] != telegram_id:
                raise ServiceError("User has no verified Telegram binding.")
            if binding.get("active") is not True:
                return _CONNECT_RETRY
            code = _safe_text(self._login_code_issuer(binding["user_key"]), maximum=128)
            return _message_text(
                onboarding_text(self._public_mcp_url, linked=True, login_code=code, client=client)
            )
        except Exception:
            return f"{response}\n\n{_CONNECT_RETRY}"

    async def _send_connect(self, telegram_id: int, author: dict[str, Any], *, client: str) -> None:
        try:
            if self._login_code_issuer is not None:
                username, first_name = self._author_profile(author)
                self._store.ensure_private_user(telegram_id, telegram_id, username, first_name)
            user = self._store.get_user_by_telegram(telegram_id)
            if user is not None:
                binding = self._binding(user["user_key"])
                if binding["telegram_user_id"] != telegram_id:
                    raise ServiceError("User has no verified Telegram binding.")
                if binding.get("active") is not True:
                    self._store.set_active_by_telegram(telegram_id, True)
        except Exception:
            response = f"{onboarding_text(self._public_mcp_url, client=client)}\n\n{_CONNECT_RETRY}"
        else:
            response = self._connect_text(telegram_id, client=client)
        if _CONNECT_ICON_NOTE not in response:
            response = f"{response}\n\n{_CONNECT_ICON_NOTE}"
        response = _message_text(response)
        entities, reply_markup = onboarding_presentation(response)
        sent = await self._reply(
            telegram_id,
            response,
            entities=entities,
            reply_markup=reply_markup,
            deactivate_on_forbidden=False,
        )
        if sent:
            try:
                await self._bot.send_connector_icon(
                    telegram_id, reply_markup=self._keyboard(telegram_id)
                )
            except (TelegramAPIError, TelegramTransportError, OSError, ValueError):
                await self._reply(
                    telegram_id,
                    _CONNECT_ICON_RETRY,
                    reply_markup=self._keyboard(telegram_id),
                    deactivate_on_forbidden=False,
                )
        await self._refresh_commands(telegram_id)

    async def _control(
        self,
        action: str,
        telegram_id: int,
        author: dict[str, Any],
        *,
        token: str | None = None,
        legacy: bool = False,
    ) -> None:
        now = monotonic()
        self._pending_unlinks = {
            key: pending for key, pending in self._pending_unlinks.items() if pending[1] > now
        }
        pending = self._pending_unlinks.pop(telegram_id, None)
        user = self._store.get_user_by_telegram(telegram_id)
        confirming_unlink = (
            action == "ui:unlink_confirm"
            and pending is not None
            and user is not None
            and pending[0] == user["user_key"]
        )
        if action in {"ui:chatgpt", "ui:claude", "ui:other"} or (legacy and action == "ui:connect"):
            await self._send_connect(
                telegram_id, author, client=action.removeprefix("ui:") if not legacy else "chatgpt"
            )
            return
        if action == "ui:connect":
            response, markup = client_picker()
        elif action in {"ui:help_chatgpt", "ui:help_claude", "ui:help_other"}:
            response, markup = help_presentation(
                action.removeprefix("ui:help_"), self._public_mcp_url
            )
        elif action == "ui:privacy":
            response, markup = privacy_presentation()
        elif action == "ui:unlink" and not legacy:
            if user is not None:
                if len(self._pending_unlinks) >= 1000:
                    del self._pending_unlinks[next(iter(self._pending_unlinks))]
                self._pending_unlinks[telegram_id] = (user["user_key"], now + 120)
            response, markup = unlink_presentation()
        elif (
            (not legacy and action in _RETIRED_CALLBACKS)
            or action == "ui:cancel"
            or (action == "ui:unlink_confirm" and not confirming_unlink)
        ):
            response = intro_text(linked=self._store.get_user_by_telegram(telegram_id) is not None)
            markup = self._keyboard(telegram_id)
            await self._reply(telegram_id, response, reply_markup=markup)
            await self._refresh_commands(telegram_id)
            return
        else:
            await self._management_control(action, telegram_id, author, token=token)
            return
        await self._reply(telegram_id, response, reply_markup=markup)

    async def _management_control(
        self, action: str, telegram_id: int, author: dict[str, Any], *, token: str | None
    ) -> None:
        name = action.removeprefix("ui:")
        if name == "start" and token is not None:
            username, first_name = self._author_profile(author)
            try:
                self._store.consume_link(token, telegram_id, telegram_id, username, first_name)
            except ValueError:
                response = _INVALID_LINK
            else:
                response = _LINKED
        elif name == "start":
            self._store.set_active_by_telegram(telegram_id, True)
            response = ""
        elif self._store.get_user_by_telegram(telegram_id) is None:
            response = _CONNECT
        elif name in {"unlink", "unlink_confirm"}:
            user = self._store.get_user_by_telegram(telegram_id)
            if user is not None:
                try:
                    self.unlink_user(user["user_key"])
                except ServiceError:
                    response = _UNLINK_RETRY
                else:
                    response = _UNLINKED
            else:
                response = _UNLINKED
        else:
            return
        if name == "start":
            introduction = intro_text(
                linked=self._store.get_user_by_telegram(telegram_id) is not None
            )
            response = f"{response}\n\n{introduction}" if response else introduction
        await self._reply(telegram_id, response, reply_markup=self._keyboard(telegram_id))
        await self._refresh_commands(telegram_id)

    async def handle_update(self, update: dict[str, Any]) -> bool:
        """Return whether an authenticated bot control was handled, excluding inbox text."""
        if not isinstance(update, dict):
            return False
        if "callback_query" in update:
            control = self._callback_control(update["callback_query"])
            if control is None:
                return False
            action, telegram_id, author, query_id = control
            try:
                await self._bot.answer_callback_query(query_id)
            except (TelegramAPIError, TelegramTransportError):
                logger.warning("Telegram callback acknowledgement could not be sent.")
            await self._control(action, telegram_id, author)
            return True
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        private = self._private_author(message)
        if private is None:
            return False
        telegram_id, author = private
        text = message.get("text")
        if not isinstance(text, str):
            return False
        action = BUTTON_ACTIONS.get(text)
        if action is not None:
            await self._control(action, telegram_id, author)
            return True
        retired = _RETIRED_COMMAND.fullmatch(text)
        if text in _RETIRED_BUTTONS or (
            retired is not None
            and (retired[1] is None or retired[1].casefold() == self._bot_username.casefold())
        ):
            await self._control("ui:menu", telegram_id, author)
            return True
        command = _COMMAND.fullmatch(text)
        if command is None:
            return False
        name, username, token = command.groups()
        if username is not None and username.casefold() != self._bot_username.casefold():
            return False
        if token is not None and name != "start":
            return False
        await self._control(f"ui:{name}", telegram_id, author, token=token, legacy=True)
        return True
