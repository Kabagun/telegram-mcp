"""Small Telegram Bot API client with safe, content-free exceptions."""

import asyncio
import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Any, Self

import httpx

from .config import DEFAULT_API_BASE_URL, MAX_ATTACHMENT_BYTES, normalize_api_base_url

TELEGRAM_MAX_FILE_BYTES = 20 * 1024 * 1024
_FILE_PATH = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)+\Z")
_SEND_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,4096}\Z")
_BOT_COMMAND = re.compile(r"[a-z0-9_]{1,32}\Z")
_ATTACHMENT_METHODS = {
    "document": "sendDocument",
    "photo": "sendPhoto",
    "audio": "sendAudio",
    "video": "sendVideo",
    "voice": "sendVoice",
    "animation": "sendAnimation",
}
_SEND_METHODS = frozenset({"sendMessage", *_ATTACHMENT_METHODS.values()})


class TelegramFileError(ValueError):
    """A bounded download failed without exposing a file identifier or URL."""

    def __init__(self) -> None:
        super().__init__("Telegram file exceeds the allowed download size.")


def _valid_file_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 1024
        and _FILE_PATH.fullmatch(value) is not None
        and all(part not in {".", ".."} for part in value.split("/"))
    )


class TelegramAPIError(Exception):
    """An API rejection whose message never includes Telegram response content."""

    def __init__(self, code: int, retry_after: int | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        self.description = {
            400: "Telegram rejected an invalid request.",
            401: "Telegram bot authentication failed.",
            403: "Telegram bot is blocked or cannot access this chat.",
            404: "Telegram API method was not found.",
            409: "Telegram polling conflicts with another poller or an active webhook.",
            429: "Telegram rate limit exceeded.",
        }.get(
            code, "Telegram service is unavailable." if code >= 500 else "Telegram request failed."
        )
        super().__init__(self.description)


class TelegramTransportError(Exception):
    """A network failure; a failed send may already have reached Telegram."""

    def __init__(self) -> None:
        super().__init__("Telegram transport failed; message delivery status may be unknown.")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class TelegramClient:
    """Bot-only API calls. Injected HTTP clients remain owned by the caller."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_base_url: str = DEFAULT_API_BASE_URL,
        local_files_root: Path | None = None,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("A Telegram bot token is required.")
        api_base_url = normalize_api_base_url(api_base_url)
        self.is_local_api = api_base_url != DEFAULT_API_BASE_URL
        self.max_file_bytes = MAX_ATTACHMENT_BYTES if self.is_local_api else TELEGRAM_MAX_FILE_BYTES
        self._local_files_root: Path | None = None
        if local_files_root is not None:
            if not self.is_local_api or not Path(local_files_root).is_absolute():
                raise ValueError("Local Telegram files require an absolute root and local Bot API.")
            try:
                root = Path(local_files_root).resolve(strict=True)
                valid_root = root.is_dir()
            except (OSError, RuntimeError, ValueError):
                valid_root = False
            if not valid_root:
                raise ValueError("Local Telegram file root is unavailable.")
            self._local_files_root = root
        self._base_url = f"{api_base_url}/bot{token}"
        self._file_base_url = f"{api_base_url}/file/bot{token}"
        self._owns_client = client is None
        self._client = (
            client if client is not None else httpx.AsyncClient(trust_env=not self.is_local_api)
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(
        self, method: str, payload: dict[str, Any], *, read_timeout: float = 10.0
    ) -> Any:
        try:
            response = await self._client.post(
                f"{self._base_url}/{method}",
                json=payload,
                timeout=httpx.Timeout(10.0, read=read_timeout),
                follow_redirects=False,
            )
        except (httpx.RequestError, httpx.InvalidURL):
            # Raising outside this handler also avoids retaining the unsafe exception context.
            response = None
        if response is None:
            raise TelegramTransportError()

        try:
            body = response.json()
        except ValueError:
            body = None

        http_success = 200 <= response.status_code < 300
        if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
            if http_success and method in _SEND_METHODS:
                raise TelegramTransportError()
            raise TelegramAPIError(response.status_code if not http_success else 502)
        if not http_success or not body["ok"]:
            code = body.get("error_code") if not body["ok"] else None
            if not _is_int(code) or not 400 <= code < 600:
                code = response.status_code if not http_success else 502
            parameters = body.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if not _is_int(retry_after) or retry_after < 0:
                retry_after = None
            raise TelegramAPIError(code, retry_after)
        if "result" not in body:
            if method in _SEND_METHODS:
                raise TelegramTransportError()
            raise TelegramAPIError(502)
        return body["result"]

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        if (
            not isinstance(result, dict)
            or not _is_int(result.get("id"))
            or result.get("is_bot") is not True
        ):
            raise TelegramAPIError(502)
        return result

    async def set_commands(
        self, commands: list[dict[str, str]], *, chat_id: int | None = None
    ) -> None:
        """Replace the default menu or one private chat's menu in a single attempt."""
        if not isinstance(commands, list) or len(commands) > 100:
            raise ValueError("Telegram command list is invalid.")
        seen: set[str] = set()
        for entry in commands:
            if not isinstance(entry, dict) or set(entry) != {"command", "description"}:
                raise ValueError("Telegram command list is invalid.")
            command, description = entry["command"], entry["description"]
            if (
                not isinstance(command, str)
                or _BOT_COMMAND.fullmatch(command) is None
                or command in seen
                or not isinstance(description, str)
                or not description.strip()
                or len(description) > 256
                or any(
                    unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                    for character in description
                )
            ):
                raise ValueError("Telegram command list is invalid.")
            if len(description.encode("utf-16-le")) // 2 > 256:
                raise ValueError("Telegram command list is invalid.")
            seen.add(command)
        scope: dict[str, Any] = {"type": "default"}
        if chat_id is not None:
            if not _is_int(chat_id) or not 1 <= chat_id <= 2**63 - 1:
                raise ValueError("Telegram command chat identifier is invalid.")
            scope = {"type": "chat", "chat_id": chat_id}
        result = await self._call("setMyCommands", {"commands": commands, "scope": scope})
        if result is not True:
            raise TelegramAPIError(502)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        entities: list[dict[str, Any]] | None = None,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send once using plain text unless explicit entities were supplied.

        Transport failures are deliberately not retried because delivery is ambiguous.
        """
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if entities is not None:
            payload["entities"] = entities
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            if not _is_int(reply_to_message_id) or not 1 <= reply_to_message_id <= 2**63 - 1:
                raise ValueError("Reply message identifier is invalid.")
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": False,
            }
        result = await self._call("sendMessage", payload)
        if (
            not isinstance(result, dict)
            or not _is_int(result.get("message_id"))
            or result["message_id"] < 1
            or not isinstance(result.get("chat"), dict)
            or not _is_int(result["chat"].get("id"))
            or result["chat"]["id"] != chat_id
        ):
            # A success envelope with an unusable acknowledgement does not
            # prove that Telegram rejected the message. Never retry it.
            raise TelegramTransportError()
        return result

    async def get_file(self, file_id: str) -> dict[str, Any]:
        """Resolve an opaque Telegram file identifier without downloading its body."""
        if (
            not isinstance(file_id, str)
            or not 1 <= len(file_id) <= 4096
            or any(not character.isascii() or character.isspace() for character in file_id)
            or any(ord(character) < 32 or ord(character) == 127 for character in file_id)
        ):
            raise ValueError("Telegram file identifier is invalid.")
        result = await self._call("getFile", {"file_id": file_id})
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("file_id"), str)
            or not result["file_id"]
            or not self._valid_download_path(result.get("file_path"))
            or (
                "file_size" in result
                and (not _is_int(result["file_size"]) or result["file_size"] < 0)
            )
        ):
            raise TelegramAPIError(502)
        return result

    async def send_attachment(
        self,
        chat_id: int,
        kind: str,
        file_id: str,
        *,
        caption: str = "",
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Resend one known Telegram file ID using its original media type.

        URLs, local paths and uploads are deliberately not supported. Captions
        are plain text, and a missing reply target must cause Telegram to reject.
        """
        if not _is_int(chat_id) or not -(2**63 - 1) <= chat_id <= 2**63 - 1 or chat_id == 0:
            raise ValueError("Telegram chat identifier is invalid.")
        if not isinstance(kind, str) or kind not in _ATTACHMENT_METHODS:
            raise ValueError("Telegram attachment type is invalid.")
        if not isinstance(file_id, str) or _SEND_FILE_ID.fullmatch(file_id) is None:
            raise ValueError("Telegram file identifier is invalid.")
        if not isinstance(caption, str) or len(caption) > 1024:
            raise ValueError("Telegram caption is invalid or exceeds the allowed length.")
        for character in caption:
            category = unicodedata.category(character)
            if category == "Cs" or (category == "Cc" and character not in "\n\t"):
                raise ValueError("Telegram caption contains unsupported characters.")
        if len(caption.encode("utf-16-le")) // 2 > 1024:
            raise ValueError("Telegram caption exceeds the allowed length.")
        payload: dict[str, Any] = {"chat_id": chat_id, kind: file_id, "caption": caption}
        if message_thread_id is not None:
            if not _is_int(message_thread_id) or not 1 <= message_thread_id <= 2**63 - 1:
                raise ValueError("Telegram topic identifier is invalid.")
            if chat_id > 0:
                raise ValueError("Telegram topic requires a group chat.")
            payload["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            if not _is_int(reply_to_message_id) or not 1 <= reply_to_message_id <= 2**63 - 1:
                raise ValueError("Reply message identifier is invalid.")
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": False,
            }
        result = await self._call(_ATTACHMENT_METHODS[kind], payload)
        if (
            not isinstance(result, dict)
            or not _is_int(result.get("message_id"))
            or not 1 <= result["message_id"] <= 2**63 - 1
            or not isinstance(result.get("chat"), dict)
            or not _is_int(result["chat"].get("id"))
            or result["chat"]["id"] != chat_id
        ):
            raise TelegramTransportError()
        return result

    def _valid_download_path(self, value: object) -> bool:
        if _valid_file_path(value):
            return True
        return (
            self._local_files_root is not None
            and isinstance(value, str)
            and len(value) <= 4096
            and "\x00" not in value
            and Path(value).is_absolute()
            and ".." not in Path(value).parts
        )

    def _read_local_file(self, file_path: str, max_bytes: int) -> bytes:
        """Resolve and verify the opened file before reading bounded chunks in a worker thread."""
        failed = False
        descriptor: int | None = None
        try:
            root = self._local_files_root
            resolved = Path(file_path).resolve(strict=True)
            if root is None or not resolved.is_relative_to(root) or not resolved.is_file():
                raise TelegramAPIError(502)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(resolved, flags)
            opened = os.fstat(descriptor)
            # Re-resolve after opening, then match the path's identity to the
            # descriptor. This also detects an intermediate directory swap.
            checked = resolved.resolve(strict=True)
            checked_stat = checked.stat()
            if (
                not checked.is_relative_to(root)
                or not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (checked_stat.st_dev, checked_stat.st_ino)
            ):
                raise TelegramAPIError(502)
            if opened.st_size > max_bytes:
                raise TelegramFileError()
            content = bytearray()
            while True:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes - len(content) + 1))
                if not chunk:
                    return bytes(content)
                if len(content) + len(chunk) > max_bytes:
                    raise TelegramFileError()
                content.extend(chunk)
        except (OSError, RuntimeError, ValueError) as error:
            if isinstance(error, TelegramFileError):
                raise
            failed = True
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if failed:
            raise TelegramAPIError(502)
        raise TelegramAPIError(502)

    async def download_file(self, file_path: str, *, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        """Read a bounded file stream from Telegram; never follow redirects or write to disk."""
        if not self._valid_download_path(file_path):
            raise ValueError("Telegram file path is invalid.")
        if not _is_int(max_bytes) or not 1 <= max_bytes <= self.max_file_bytes:
            raise ValueError("Telegram download limit is invalid.")
        if self._local_files_root is not None and Path(file_path).is_absolute():
            return await asyncio.to_thread(self._read_local_file, file_path, max_bytes)
        failed_transport = False
        try:
            async with self._client.stream(
                "GET",
                f"{self._file_base_url}/{file_path}",
                timeout=httpx.Timeout(10.0, read=30.0),
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    code = response.status_code if 400 <= response.status_code < 600 else 502
                    raise TelegramAPIError(code)
                length = response.headers.get("content-length")
                if (
                    length is not None
                    and length.isdecimal()
                    and (len(length) > 20 or int(length) > max_bytes)
                ):
                    raise TelegramFileError()
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(content) + len(chunk) > max_bytes:
                        raise TelegramFileError()
                    content.extend(chunk)
                return bytes(content)
        except (httpx.RequestError, httpx.InvalidURL):
            failed_transport = True
        if failed_transport:
            raise TelegramTransportError()
        raise TelegramAPIError(502)  # Defensive: a response must complete or fail above.

    async def get_updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:  # noqa: ASYNC109
        # This is Telegram's server-side long-poll timeout, not a coroutine timeout.
        result = await self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "my_chat_member"],
            },
            read_timeout=timeout + 10.0,
        )
        if not isinstance(result, list) or any(
            not isinstance(update, dict)
            or not _is_int(update.get("update_id"))
            or update["update_id"] < 0
            for update in result
        ):
            raise TelegramAPIError(502)
        return result
