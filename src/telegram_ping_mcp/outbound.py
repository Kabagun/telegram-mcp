"""One-attempt, idempotent outbound sends to verified private profiles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from .config import MAX_ATTACHMENT_BYTES
from .file_inputs import FileDownloader, FileInput, parse_file_input
from .inbox import InboundController, InboxStore
from .service import Service, ServiceError, _integer, _safe_text, _utf16_length
from .store import Store
from .telegram import TelegramAPIError, TelegramClient, TelegramTransportError

_UNAVAILABLE = "Telegram delivery is unavailable for this verified user."
_REPLY_UNAVAILABLE = "Reply target is unavailable for this verified user or private chat."
_ATTACHMENT_UNAVAILABLE = "Attachment is unavailable for this verified user or private chat."
_ATTACHMENT_FAILED = "Telegram attachment metadata is unavailable."
_FAILED = "Telegram rejected the message."
_UNKNOWN = "Telegram delivery status is unknown."
_PREPARATION_FAILED = "Attached file could not be prepared."
_PREPARATION_ERRORS = {
    _UNAVAILABLE,
    _ATTACHMENT_FAILED,
    "Attached file could not be downloaded.",
    "Attached file is empty.",
    "Attachment exceeds the allowed send size.",
    "Numeric identifier or limit is invalid.",
}
_KINDS = {"document", "photo", "audio", "video", "voice", "animation"}


class OutboundSender:
    def __init__(
        self,
        store: Store,
        bot: TelegramClient,
        inbox: InboxStore,
        inbound: InboundController,
        *,
        max_attachment_bytes: int,
        file_downloader: FileDownloader | None = None,
        download_lock: asyncio.Lock | None = None,
    ) -> None:
        if store.path.resolve() != inbox.path.resolve():
            raise ValueError("Outbound sends must use the verified users database.")
        self._store = store
        self._bot = bot
        self._inbox = inbox
        self._inbound = inbound
        self.max_attachment_bytes = _integer(max_attachment_bytes, maximum=MAX_ATTACHMENT_BYTES)
        self._file_downloader = file_downloader or FileDownloader()
        self._download_lock = download_lock if download_lock is not None else asyncio.Lock()

    def _binding(self, user_key: str) -> dict[str, Any]:
        user = self._store.get_user(user_key)
        if user is None or user.get("active") is not True:
            raise ServiceError(_UNAVAILABLE)
        telegram_id = _integer(user.get("telegram_user_id"))
        if _integer(user.get("chat_id")) != telegram_id:
            raise ServiceError(_UNAVAILABLE)
        return user

    @staticmethod
    def _belongs(item: dict[str, Any], user: dict[str, Any]) -> bool:
        return (
            item.get("user_key") == user["user_key"]
            and item.get("telegram_user_id") == user["telegram_user_id"]
            and item.get("chat_id") == user["chat_id"]
            and item.get("topic_id") is None
        )

    def _revalidate(self, user: dict[str, Any]) -> None:
        current = self._binding(user["user_key"])
        if any(current[key] != user[key] for key in ("user_key", "telegram_user_id", "chat_id")):
            raise ServiceError(_UNAVAILABLE)

    async def send(
        self,
        *,
        user_key: str,
        request_id: str,
        text: str = "",
        message_id: int | None = None,
        attachment_id: str | None = None,
        file: dict | None = None,
        send_at: str | None = None,
        delay_seconds: int | None = None,
    ) -> dict[str, Any]:
        if send_at is not None or delay_seconds is not None:
            raise ServiceError(
                "Scheduled sends are not available under the current storage policy."
            )
        return await self.send_now(
            user_key=user_key,
            request_id=request_id,
            text=text,
            message_id=message_id,
            attachment_id=attachment_id,
            file=file,
        )

    async def send_now(
        self,
        *,
        user_key: str,
        request_id: str,
        text: str = "",
        message_id: int | None = None,
        attachment_id: str | None = None,
        file: dict | None = None,
    ) -> dict[str, Any]:
        user_key = _safe_text(user_key, maximum=128)
        request_id = _safe_text(request_id, maximum=128)
        if attachment_id is not None and file is not None:
            raise ServiceError("Choose one attachment per message.")
        has_attachment = attachment_id is not None or file is not None
        limit = 1024 if has_attachment else 4096
        text = _safe_text(text, maximum=limit, multiline=True, allow_blank=has_attachment)
        if _utf16_length(text) > limit:
            raise ServiceError("Message or caption exceeds the allowed length.")
        user = self._binding(user_key)
        reply_id = None
        if message_id is not None:
            target = self._inbox.get_message(_integer(message_id))
            if target is None or not self._belongs(target, user):
                raise ServiceError(_REPLY_UNAVAILABLE)
            reply_id = _integer(target["telegram_message_id"])
        item = None
        upload = None
        file_identity = None
        if attachment_id is not None:
            item = self._inbox._get_attachment(attachment_id)
            if item is None or not self._belongs(item, user):
                raise ServiceError(_ATTACHMENT_UNAVAILABLE)
            if (
                item["attachment_kind"] not in _KINDS
                or not isinstance(item["telegram_file_id"], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", item["telegram_file_id"]) is None
            ):
                raise ServiceError(_ATTACHMENT_UNAVAILABLE)
            if item["attachment_size"] is not None:
                self._check_size(item["attachment_size"])
            file_identity = {
                "attachment_id": attachment_id,
                "kind": item["attachment_kind"],
                "file_id": item["telegram_file_id"],
            }
        elif file is not None:
            upload = parse_file_input(file)
            file_identity = upload.fingerprint_data()
        payload = {
            "type": "outbound_private",
            "user_key": user_key,
            "telegram_user_id": user["telegram_user_id"],
            "chat_id": user["chat_id"],
            "text": text,
            "reply_to_message_id": reply_id,
            "file": file_identity,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            delivery = self._store.reserve_delivery(request_id, fingerprint)
        except ValueError:
            raise ServiceError("Request ID was already used for a different delivery.") from None
        if not delivery["is_new"]:
            return Service._result(request_id, delivery, duplicate=True)
        if upload is None:
            return await self._send_reserved(
                user=user,
                request_id=request_id,
                text=text,
                reply_id=reply_id,
                item=item,
                upload=None,
            )
        # Reserve before waiting so repeats can return without taking this lock.
        try:
            await self._download_lock.acquire()
        except asyncio.CancelledError:
            self._store.finish_delivery(
                request_id, "failed", error="Send preparation was cancelled."
            )
            raise
        try:
            return await self._send_reserved(
                user=user,
                request_id=request_id,
                text=text,
                reply_id=reply_id,
                item=None,
                upload=upload,
            )
        finally:
            self._download_lock.release()

    async def _send_reserved(
        self,
        *,
        user: dict[str, Any],
        request_id: str,
        text: str,
        reply_id: int | None,
        item: dict[str, Any] | None,
        upload: FileInput | None,
    ) -> dict[str, Any]:
        data = None
        try:
            # A queued upload may have been revoked while waiting for memory.
            self._revalidate(user)
            if upload is not None:
                data = await self._file_downloader.download(
                    upload, max_bytes=self.max_attachment_bytes
                )
                if not isinstance(data, bytes):
                    raise ServiceError("Attached file could not be downloaded.")
                if not data:
                    raise ServiceError("Attached file is empty.")
                self._check_size(len(data))
            elif item is not None and item["attachment_size"] is None:
                try:
                    metadata = await self._bot.get_file(item["telegram_file_id"])
                    if (
                        not isinstance(metadata, dict)
                        or metadata.get("file_id") != item["telegram_file_id"]
                    ):
                        raise ServiceError(_ATTACHMENT_FAILED)
                    self._check_size(metadata.get("file_size"))
                except ServiceError:
                    raise
                except Exception:
                    raise ServiceError(_ATTACHMENT_FAILED) from None
            # Any preparation awaited above may have allowed revocation or relinking.
            self._revalidate(user)
        except asyncio.CancelledError:
            self._store.finish_delivery(
                request_id, "failed", error="Send preparation was cancelled."
            )
            raise
        except ServiceError as error:
            data = None
            safe_error = str(error) if str(error) in _PREPARATION_ERRORS else _PREPARATION_FAILED
            self._store.finish_delivery(request_id, "failed", error=safe_error)
            raise ServiceError(safe_error) from None
        except Exception:
            data = None
            self._store.finish_delivery(request_id, "failed", error=_PREPARATION_FAILED)
            raise ServiceError(_PREPARATION_FAILED) from None
        options = {"reply_to_message_id": reply_id} if reply_id is not None else {}
        try:
            if upload is not None:
                message = await self._bot.send_document_bytes(
                    user["chat_id"],
                    data,
                    file_name=upload.file_name,
                    mime_type=upload.mime_type,
                    caption=text,
                    **options,
                )
            elif item is not None:
                message = await self._bot.send_attachment(
                    user["chat_id"],
                    item["attachment_kind"],
                    item["telegram_file_id"],
                    caption=text,
                    **options,
                )
            else:
                message = await self._bot.send_message(user["chat_id"], text, **options)
        except TelegramAPIError as error:
            self._store.finish_delivery(request_id, "failed", error=_FAILED)
            if error.code == 403:
                self._store.set_active_by_telegram(user["telegram_user_id"], False)
            return Service._result(
                request_id, {"status": "failed", "error": _FAILED}, duplicate=False
            )
        except TelegramTransportError:
            self._store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
            return Service._result(
                request_id, {"status": "unknown", "error": _UNKNOWN}, duplicate=False
            )
        except asyncio.CancelledError:
            self._store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
            raise
        except Exception:
            self._store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
            raise ServiceError(_UNKNOWN) from None
        finally:
            # Release our upload bytes before the aggregate-memory lock is released.
            data = None
        if (
            not isinstance(message, dict)
            or isinstance(message.get("message_id"), bool)
            or not isinstance(message.get("message_id"), int)
            or not 1 <= message["message_id"] <= 2**63 - 1
        ):
            self._store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
            return Service._result(
                request_id, {"status": "unknown", "error": _UNKNOWN}, duplicate=False
            )
        self._store.finish_delivery(request_id, "sent", message_id=message["message_id"])
        return Service._result(
            request_id, {"status": "sent", "message_id": message["message_id"]}, duplicate=False
        )

    def _check_size(self, size: object) -> None:
        if _integer(size, minimum=0) > self.max_attachment_bytes:
            raise ServiceError("Attachment exceeds the allowed send size.")
