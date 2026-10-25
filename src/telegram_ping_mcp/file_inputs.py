"""Validate ChatGPT file adapters and download bounded files only into memory."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import httpx

from .config import MAX_ATTACHMENT_BYTES
from .inbox import _filename
from .service import ServiceError, _integer

_INVALID = "File input is invalid; use an attached file supplied by the client."
_UNAVAILABLE = "Attached file could not be downloaded."
_TOO_LARGE = "Attachment exceeds the allowed send size."
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_FILE_KEYS = {"download_url", "file_id", "mime_type", "file_name"}
_UPLOAD_MIME = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*\Z")


@dataclass(frozen=True)
class FileInput:
    # Signed URLs and client identifiers must never appear in an object's repr.
    download_url: str = field(repr=False)
    file_id: str = field(repr=False)
    source_identity: str = field(repr=False)
    file_name: str
    mime_type: str

    def fingerprint_data(self) -> dict[str, str]:
        """Stable client identity excludes a signed URL's expiring query string."""
        return {
            "file_id": self.file_id,
            "source": self.source_identity,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
        }


def _file_url(value: object) -> tuple[str, str]:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or not value.isascii()
        or any(character.isspace() or ord(character) < 32 for character in value)
        or "\x7f" in value
        or "\\" in value
        or "#" in value
    ):
        raise ServiceError(_INVALID)
    decoded = unquote(value)
    if any(unicodedata.category(character)[0] == "C" for character in decoded) or "\\" in decoded:
        raise ServiceError(_INVALID)
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or parts.netloc.lower() != host
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
            or not host.endswith(".oaiusercontent.com")
            or len(host) > 253
            or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
            or not parts.path.startswith("/")
        ):
            raise ServiceError(_INVALID)
    except ValueError:
        raise ServiceError(_INVALID) from None
    return host, f"https://{host}{parts.path}"


def parse_file_input(value: object) -> FileInput:
    """Accept only the native client's file adapter, never paths or Telegram IDs."""
    if (
        not isinstance(value, dict)
        or not {"download_url", "file_id"}.issubset(value)
        or set(value) - _FILE_KEYS
    ):
        raise ServiceError(_INVALID)
    file_id = value["file_id"]
    if (
        not isinstance(file_id, str)
        or not 1 <= len(file_id) <= 1024
        or any(unicodedata.category(character)[0] == "C" for character in file_id)
        or not file_id.strip()
    ):
        raise ServiceError(_INVALID)
    _, source = _file_url(value["download_url"])
    name = value.get("file_name", "attachment")
    mime = value.get("mime_type", "application/octet-stream")
    if (
        not isinstance(name, str)
        or len(name) > 1024
        or not isinstance(mime, str)
        or len(mime) > 128
    ):
        raise ServiceError(_INVALID)
    # Telegram's multipart transport caps bytes rather than Unicode characters.
    safe_name = _filename(name, "attachment").encode("utf-8")[:255].decode("utf-8", errors="ignore")
    safe_mime = mime if _UPLOAD_MIME.fullmatch(mime) else "application/octet-stream"
    return FileInput(
        download_url=value["download_url"],
        file_id=file_id,
        source_identity=source,
        file_name=safe_name,
        mime_type=safe_mime,
    )


async def _resolve(host: str) -> Sequence[str]:
    records = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return tuple(dict.fromkeys(record[4][0] for record in records))


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """Connect to the checked IP while verifying the provider's TLS hostname."""

    def __init__(self, host: str, address: str) -> None:
        super().__init__(trust_env=False, retries=0)
        self._host = host
        self._address = address

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=self._address),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self._host},
        )
        pinned.headers["host"] = self._host
        return await super().handle_async_request(pinned)


class FileDownloader:
    """No redirects, proxy environment, disk files, or unbounded response bodies.

    The optional factory and resolver support isolated tests. Production clients
    use the pinned transport so DNS cannot change between validation and connect.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
        resolver: Callable[[str], Awaitable[Sequence[str]]] | None = None,
    ) -> None:
        self._client_factory = client_factory or httpx.AsyncClient
        self._resolver = resolver or _resolve

    async def download(self, file: FileInput, *, max_bytes: int) -> bytes:
        _integer(max_bytes, maximum=MAX_ATTACHMENT_BYTES)
        host, _ = _file_url(file.download_url)
        try:
            async with asyncio.timeout(45):
                addresses = await self._resolver(host)
                if not addresses or any(
                    "%" in address or not ipaddress.ip_address(address).is_global
                    for address in addresses
                ):
                    raise ServiceError(_UNAVAILABLE)
                transport = _PinnedTransport(host, str(ipaddress.ip_address(addresses[0])))
                async with self._client_factory(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(10.0, read=30.0),
                ) as client:
                    async with client.stream(
                        "GET", file.download_url, headers={"Accept-Encoding": "identity"}
                    ) as response:
                        if (
                            response.status_code != 200
                            or len(response.headers) > 100
                            or sum(len(key) + len(value) for key, value in response.headers.raw)
                            > 16 * 1024
                            or response.headers.get("content-encoding", "identity").lower()
                            != "identity"
                        ):
                            raise ServiceError(_UNAVAILABLE)
                        length = response.headers.get("content-length")
                        if length is not None:
                            if not re.fullmatch(r"[0-9]{1,20}", length):
                                raise ServiceError(_UNAVAILABLE)
                            if int(length) > max_bytes:
                                raise ServiceError(_TOO_LARGE)
                        data = bytearray()
                        async for chunk in response.aiter_raw(
                            chunk_size=min(64 * 1024, max_bytes + 1)
                        ):
                            if len(data) + len(chunk) > max_bytes:
                                raise ServiceError(_TOO_LARGE)
                            data.extend(chunk)
                        if length is not None and int(length) != len(data):
                            raise ServiceError(_UNAVAILABLE)
                        return bytes(data)
        except ServiceError:
            raise
        except Exception:
            # Neither signed source URLs nor transport exception content escape.
            raise ServiceError(_UNAVAILABLE) from None
