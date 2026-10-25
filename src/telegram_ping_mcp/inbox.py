"""Verified inbound messages and on-demand, bounded Telegram attachment reads."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import MAX_ATTACHMENT_BYTES
from .service import Service, ServiceError, _safe_text, _utf16_length
from .store import Store
from .telegram import (
    TELEGRAM_MAX_FILE_BYTES,
    TelegramAPIError,
    TelegramClient,
    TelegramFileError,
    TelegramTransportError,
)

_MAX_INTEGER = 2**63 - 1
_DEFAULT_FILE_LIMIT = MAX_ATTACHMENT_BYTES
_ATTACHMENT_KINDS = {"document", "photo", "audio", "video", "voice", "animation"}
_UNTRUSTED_FIELDS = {
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
_UNSUPPORTED_CONTENT = {
    "sticker",
    "video_note",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
    "paid_media",
    "story",
    "game",
    "invoice",
    "successful_payment",
    "passport_data",
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "pinned_message",
    "giveaway",
    "giveaway_winners",
}
_MIME = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")
_ATTACHMENT_ID = re.compile(r"[A-Za-z0-9_-]{32}\Z")
_FILE_UNAVAILABLE = "Attachment is unavailable for this verified user or chat."
_FILE_FAILED = "Telegram attachment could not be downloaded."
_REPLY_FAILED = "Telegram rejected the message."
_REPLY_UNKNOWN = "Telegram delivery status is unknown."
_REPLY_FILE_FAILED = "Telegram attachment metadata is unavailable."


def _integer(value: object, *, minimum: int = 1, maximum: int = _MAX_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ServiceError("Numeric identifier or limit is invalid.")
    return value


def _body(value: object, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ServiceError("Inbound text is invalid or exceeds the allowed length.")
    for character in value:
        category = unicodedata.category(character)
        if category == "Cs" or (category == "Cc" and character not in "\n\t"):
            raise ServiceError("Inbound text contains unsupported characters.")
    if len(value.encode("utf-16-le")) // 2 > limit:
        raise ServiceError("Inbound text exceeds the allowed length.")
    return value


def _file_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or any(not char.isascii() or char.isspace() or ord(char) < 32 for char in value)
        or "\x7f" in value
    ):
        raise ServiceError("Attachment identifier is invalid.")
    return value


def _filename(value: object, fallback: str) -> str:
    # Metadata only: this name is never joined to a filesystem path.
    if not isinstance(value, str):
        return fallback
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if unicodedata.category(char)[0] != "C").strip()
    name = name[:255]
    return name if name not in {"", ".", ".."} else fallback


def _mime_type(value: object, fallback: str) -> str:
    if isinstance(value, str) and len(value) <= 128 and _MIME.fullmatch(value):
        return value
    return fallback


class InboxStore:
    """Separate short SQLite operations in the same database as verified bindings.

    Message bodies and file metadata are stored; attachment bytes and download URLs
    are never persisted. Historical messages remain readable after opting out.
    """

    def __init__(self, path: str | Path, now: Callable[[], float] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or time.time
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbound_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_key TEXT NOT NULL,
                    telegram_user_id INTEGER NOT NULL CHECK (telegram_user_id > 0),
                    chat_id INTEGER NOT NULL CHECK (chat_id != 0),
                    telegram_message_id INTEGER NOT NULL CHECK (telegram_message_id > 0),
                    topic_id INTEGER,
                    sent_at INTEGER,
                    received_at REAL NOT NULL,
                    text TEXT,
                    caption TEXT,
                    attachment_id TEXT UNIQUE,
                    attachment_kind TEXT,
                    attachment_name TEXT,
                    attachment_mime_type TEXT,
                    attachment_size INTEGER,
                    telegram_file_id TEXT,
                    is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
                    UNIQUE (chat_id, telegram_message_id)
                );
                CREATE INDEX IF NOT EXISTS inbound_messages_user ON inbound_messages (user_key, id);
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            key: row[key]
            for key in (
                "id",
                "user_key",
                "telegram_user_id",
                "chat_id",
                "telegram_message_id",
                "topic_id",
                "sent_at",
                "received_at",
                "text",
                "caption",
            )
        }
        result["read"] = bool(row["is_read"])
        result["attachment"] = None
        if row["attachment_id"] is not None:
            result["attachment"] = {
                "id": row["attachment_id"],
                "kind": row["attachment_kind"],
                "name": row["attachment_name"],
                "mime_type": row["attachment_mime_type"],
                "size": row["attachment_size"],
            }
        return result

    def _save(
        self,
        *,
        user_key: str,
        telegram_user_id: int,
        chat_id: int,
        telegram_message_id: int,
        topic_id: int | None,
        sent_at: int | None,
        text: str | None,
        caption: str | None,
        attachment: dict[str, Any] | None,
    ) -> None:
        attachment = attachment or {}
        # INSERT SELECT makes the active-binding check atomic with persistence.
        with self._connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO inbound_messages (
                    user_key, telegram_user_id, chat_id, telegram_message_id, topic_id,
                    sent_at, received_at, text, caption, attachment_id, attachment_kind,
                    attachment_name, attachment_mime_type, attachment_size, telegram_file_id
                ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                  FROM users WHERE user_key = ? AND telegram_user_id = ?
                      AND chat_id = telegram_user_id AND active = 1""",
                (
                    user_key,
                    telegram_user_id,
                    chat_id,
                    telegram_message_id,
                    topic_id,
                    sent_at,
                    self._now(),
                    text,
                    caption,
                    attachment.get("id"),
                    attachment.get("kind"),
                    attachment.get("name"),
                    attachment.get("mime_type"),
                    attachment.get("size"),
                    attachment.get("file_id"),
                    user_key,
                    telegram_user_id,
                ),
            )

    def list_messages(
        self,
        user_key: str | None = None,
        chat_id: int | None = None,
        after_id: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        _integer(after_id, minimum=0)
        _integer(limit, maximum=100)
        if user_key is not None and (
            not isinstance(user_key, str) or not user_key.strip() or len(user_key) > 128
        ):
            raise ServiceError("User key is invalid.")
        if chat_id is not None:
            _integer(chat_id, minimum=-_MAX_INTEGER)
            if chat_id == 0:
                raise ServiceError("Chat identifier is invalid.")
        conditions = ["id > ?"]
        parameters: list[Any] = [after_id]
        if user_key is not None:
            conditions.append("user_key = ?")
            parameters.append(user_key)
        if chat_id is not None:
            conditions.append("chat_id = ?")
            parameters.append(chat_id)
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM inbound_messages WHERE {' AND '.join(conditions)} "
                "ORDER BY id LIMIT ?",
                parameters,
            ).fetchall()
        return [self._public(row) for row in rows]

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        _integer(message_id)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM inbound_messages WHERE id = ?", (message_id,))
            message = row.fetchone()
        return self._public(message) if message is not None else None

    def mark_read(self, message_id: int) -> dict[str, Any]:
        _integer(message_id)
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE inbound_messages SET is_read = 1 WHERE id = ?", (message_id,)
            )
        return {"message_id": message_id, "read": bool(cursor.rowcount)}

    def _get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        if not isinstance(attachment_id, str) or _ATTACHMENT_ID.fullmatch(attachment_id) is None:
            raise ServiceError("Attachment identifier is invalid.")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_messages WHERE attachment_id = ?", (attachment_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def _get_attachment_message(self, message_id: int) -> dict[str, Any] | None:
        _integer(message_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_messages WHERE id = ? AND attachment_id IS NOT NULL",
                (message_id,),
            ).fetchone()
        return dict(row) if row is not None else None


class InboundController:
    """Ingest only ordinary messages from currently verified, active human users."""

    def __init__(
        self,
        store: Store,
        inbox: InboxStore,
        bot: TelegramClient,
        *,
        allowed_group_chat_ids: frozenset[int] = frozenset(),
        max_attachment_bytes: int = _DEFAULT_FILE_LIMIT,
    ) -> None:
        if store.path.resolve() != inbox.path.resolve():
            raise ValueError("Inbound messages must use the verified users database.")
        self._store = store
        self._inbox = inbox
        self._bot = bot
        self._allowed_group_chat_ids = allowed_group_chat_ids
        self.max_attachment_bytes = _integer(max_attachment_bytes, maximum=MAX_ATTACHMENT_BYTES)

    @staticmethod
    def _attachment(message: dict[str, Any]) -> dict[str, Any] | None:
        kinds = _ATTACHMENT_KINDS.intersection(message)
        # Telegram represents an animation both as animation and document.
        if "animation" in kinds:
            kinds.discard("document")
        if not kinds:
            return None
        if len(kinds) != 1:
            raise ServiceError("Attachment metadata is invalid.")
        kind = kinds.pop()
        value = message[kind]
        if kind == "photo":
            if not isinstance(value, list) or not value:
                raise ServiceError("Attachment metadata is invalid.")
            value = value[-1]
        if not isinstance(value, dict):
            raise ServiceError("Attachment metadata is invalid.")
        file_id = _file_id(value.get("file_id"))
        size = value.get("file_size")
        if size is not None:
            _integer(size, minimum=0)
        mime, name = {
            "photo": ("image/jpeg", "photo.jpg"),
            "document": ("application/octet-stream", "document"),
            "audio": ("audio/mpeg", "audio.mp3"),
            "video": ("video/mp4", "video.mp4"),
            "voice": ("audio/ogg", "voice.ogg"),
            "animation": ("video/mp4", "animation.mp4"),
        }[kind]
        return {
            "id": secrets.token_urlsafe(24),
            "kind": kind,
            "file_id": file_id,
            "size": size,
            "mime_type": _mime_type(value.get("mime_type"), mime),
            "name": _filename(value.get("file_name"), name),
        }

    async def handle_update(self, update: dict[str, Any]) -> None:
        if not isinstance(update, dict) or not isinstance(update.get("message"), dict):
            return
        message = update["message"]
        if _UNTRUSTED_FIELDS.intersection(message) or _UNSUPPORTED_CONTENT.intersection(message):
            return
        author, chat = message.get("from"), message.get("chat")
        if (
            not isinstance(author, dict)
            or not isinstance(chat, dict)
            or author.get("is_bot") is not False
        ):
            return
        try:
            author_id = _integer(author.get("id"))
            chat_id = _integer(chat.get("id"), minimum=-_MAX_INTEGER)
            kind = chat.get("type")
            if kind == "private":
                if chat_id != author_id:
                    return
            elif kind in {"group", "supergroup"}:
                if chat_id >= 0 or chat_id not in self._allowed_group_chat_ids:
                    return
            else:
                return
            user = self._store.get_user_by_telegram(author_id)
            if user is None or user["active"] is not True or user["chat_id"] != author_id:
                return
            message_id = _integer(message.get("message_id"))
            topic = message.get("message_thread_id")
            if topic is not None:
                _integer(topic)
                if kind == "private":
                    return
            sent_at = message.get("date")
            if sent_at is not None:
                _integer(sent_at, minimum=0)
            text = _body(message["text"], 4096) if "text" in message else None
            caption = _body(message["caption"], 1024) if "caption" in message else None
            if (text is not None and text.lstrip().startswith("/")) or (
                caption is not None and caption.lstrip().startswith("/")
            ):
                return
            attachment = self._attachment(message)
            if attachment is None and (text is None or not text.strip()):
                return
            self._inbox._save(
                user_key=user["user_key"],
                telegram_user_id=author_id,
                chat_id=chat_id,
                telegram_message_id=message_id,
                topic_id=topic,
                sent_at=sent_at,
                text=text,
                caption=caption,
                attachment=attachment,
            )
        except (ServiceError, ValueError):
            return

    def _attachment_authorized(self, item: dict[str, Any]) -> bool:
        user = self._store.get_user(item["user_key"])
        return (
            user is not None
            and user["active"] is True
            and user["telegram_user_id"] == item["telegram_user_id"]
            and user["chat_id"] == item["telegram_user_id"]
            and (
                item["chat_id"] == item["telegram_user_id"]
                or item["chat_id"] in self._allowed_group_chat_ids
            )
        )

    async def reply_attachment(
        self, message_id: int, caption: str, request_id: str
    ) -> dict[str, Any]:
        """Resend the exact accepted incoming file to its original chat and topic."""
        item = self._inbox._get_attachment_message(message_id)
        if item is None or not self._attachment_authorized(item):
            raise ServiceError(_FILE_UNAVAILABLE)
        caption = _safe_text(caption, maximum=1024, multiline=True, allow_blank=True)
        if _utf16_length(caption) > 1024:
            raise ServiceError("Caption exceeds the allowed length.")
        request_id = _safe_text(request_id, maximum=128)
        telegram_id = _integer(item["telegram_user_id"])
        chat_id = _integer(item["chat_id"], minimum=-_MAX_INTEGER)
        if chat_id != telegram_id and (chat_id >= 0 or chat_id not in self._allowed_group_chat_ids):
            raise ServiceError(_FILE_UNAVAILABLE)
        reply_id = _integer(item["telegram_message_id"])
        topic_id = item["topic_id"]
        if topic_id is not None:
            _integer(topic_id)
            if chat_id > 0:
                raise ServiceError(_FILE_UNAVAILABLE)
        kind = item["attachment_kind"]
        file_id = _file_id(item["telegram_file_id"])
        if kind not in _ATTACHMENT_KINDS or re.fullmatch(r"[A-Za-z0-9_-]+", file_id) is None:
            raise ServiceError("Attachment metadata is invalid.")
        max_bytes = min(self.max_attachment_bytes, self._bot_file_limit())
        size = item["attachment_size"]
        if size is None:
            try:
                metadata = await self._bot.get_file(file_id)
                if not isinstance(metadata, dict) or metadata.get("file_id") != file_id:
                    raise ServiceError(_REPLY_FILE_FAILED)
                size = _integer(metadata.get("file_size"), minimum=0)
            except Exception:
                raise ServiceError(_REPLY_FILE_FAILED) from None
            if not self._attachment_authorized(item):
                raise ServiceError(_FILE_UNAVAILABLE)
        else:
            _integer(size, minimum=0)
        if size > max_bytes:
            if size > TELEGRAM_MAX_FILE_BYTES and self._bot_file_limit() <= TELEGRAM_MAX_FILE_BYTES:
                raise ServiceError(
                    "Files above 20 MiB require a configured local Telegram Bot API server."
                )
            raise ServiceError("Attachment exceeds the allowed send size.")
        fingerprint = Service._fingerprint(
            "attachment_reply",
            chat_id,
            [item],
            json.dumps(
                {"message_id": message_id, "kind": kind, "file_id": file_id, "caption": caption},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            topic_id,
            reply_id,
        )
        try:
            delivery = self._store.reserve_delivery(request_id, fingerprint)
        except ValueError:
            raise ServiceError("Request ID was already used for a different delivery.") from None
        if not delivery["is_new"]:
            return Service._result(request_id, delivery, duplicate=True)
        # Recheck immediately before sending, including after metadata resolution.
        if not self._attachment_authorized(item):
            self._store.finish_delivery(request_id, "failed", error=_FILE_UNAVAILABLE)
            raise ServiceError(_FILE_UNAVAILABLE)
        try:
            message = await self._bot.send_attachment(
                chat_id,
                kind,
                file_id,
                caption=caption,
                message_thread_id=topic_id,
                reply_to_message_id=reply_id,
            )
        except TelegramAPIError as error:
            self._store.finish_delivery(request_id, "failed", error=_REPLY_FAILED)
            if chat_id > 0 and error.code == 403:
                self._store.set_active_by_telegram(telegram_id, False)
            return Service._result(
                request_id, {"status": "failed", "error": _REPLY_FAILED}, duplicate=False
            )
        except TelegramTransportError:
            self._store.finish_delivery(request_id, "unknown", error=_REPLY_UNKNOWN)
            return Service._result(
                request_id, {"status": "unknown", "error": _REPLY_UNKNOWN}, duplicate=False
            )
        except asyncio.CancelledError:
            self._store.finish_delivery(request_id, "unknown", error=_REPLY_UNKNOWN)
            raise
        except Exception:
            self._store.finish_delivery(request_id, "unknown", error=_REPLY_UNKNOWN)
            raise ServiceError(_REPLY_UNKNOWN) from None
        if (
            not isinstance(message, dict)
            or isinstance(message.get("message_id"), bool)
            or not isinstance(message.get("message_id"), int)
            or not 1 <= message["message_id"] <= _MAX_INTEGER
        ):
            self._store.finish_delivery(request_id, "unknown", error=_REPLY_UNKNOWN)
            return Service._result(
                request_id, {"status": "unknown", "error": _REPLY_UNKNOWN}, duplicate=False
            )
        self._store.finish_delivery(request_id, "sent", message_id=message["message_id"])
        return Service._result(
            request_id, {"status": "sent", "message_id": message["message_id"]}, duplicate=False
        )

    async def read_attachment(
        self,
        attachment_id: str,
        *,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        requested_limit = self.max_attachment_bytes if max_bytes is None else max_bytes
        _integer(requested_limit, maximum=self.max_attachment_bytes)
        max_bytes = min(requested_limit, self._bot_file_limit())
        item = self._inbox._get_attachment(attachment_id)
        if item is None or not self._attachment_authorized(item):
            raise ServiceError(_FILE_UNAVAILABLE)
        if item["attachment_size"] is not None and item["attachment_size"] > max_bytes:
            self._raise_file_limit(item["attachment_size"])
        try:
            metadata = await self._bot.get_file(item["telegram_file_id"])
            size = metadata.get("file_size")
            if size is not None and _integer(size, minimum=0) > max_bytes:
                self._raise_file_limit(size)
            # Recheck after the network operation in case availability or binding changed.
            if not self._attachment_authorized(item):
                raise ServiceError(_FILE_UNAVAILABLE)
            data = await self._bot.download_file(metadata["file_path"], max_bytes=max_bytes)
        except TelegramFileError:
            self._raise_file_limit(max_bytes + 1)
        except ServiceError:
            raise
        except Exception:
            # The Telegram client strips API/network response content; this also
            # hides file metadata and injected client's exception messages.
            raise ServiceError(_FILE_FAILED) from None
        if not isinstance(data, bytes) or len(data) > max_bytes:
            raise ServiceError(_FILE_FAILED)
        if not self._attachment_authorized(item):
            raise ServiceError(_FILE_UNAVAILABLE)
        return {
            "attachment_id": attachment_id,
            "message_id": item["id"],
            "data": data,
            "mime_type": item["attachment_mime_type"],
            "name": item["attachment_name"],
            "size": len(data),
        }

    def _bot_file_limit(self) -> int:
        limit = getattr(self._bot, "max_file_bytes", TELEGRAM_MAX_FILE_BYTES)
        return _integer(limit, maximum=MAX_ATTACHMENT_BYTES)

    def _raise_file_limit(self, size: int) -> None:
        if size > TELEGRAM_MAX_FILE_BYTES and self._bot_file_limit() <= TELEGRAM_MAX_FILE_BYTES:
            raise ServiceError(
                "Files above 20 MiB require a configured local Telegram Bot API server."
            ) from None
        raise ServiceError("Attachment exceeds the allowed download size.") from None

    def attachment_info(self, attachment_id: str) -> dict[str, Any]:
        """Describe an authorized attachment without resolving or downloading its file."""
        item = self._inbox._get_attachment(attachment_id)
        if item is None or not self._attachment_authorized(item):
            raise ServiceError(_FILE_UNAVAILABLE)
        size = item["attachment_size"]
        return {
            "attachment_id": attachment_id,
            "message_id": item["id"],
            "name": item["attachment_name"],
            "mime_type": item["attachment_mime_type"],
            "size": size,
            "max_download_bytes": min(self.max_attachment_bytes, self._bot_file_limit()),
            "requires_local_bot_api": size is not None
            and size > TELEGRAM_MAX_FILE_BYTES
            and self._bot_file_limit() <= TELEGRAM_MAX_FILE_BYTES,
            "download_path": f"/attachments/{attachment_id}",
        }
