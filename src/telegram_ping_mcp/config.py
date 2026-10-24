"""Environment-backed configuration with validation that avoids leaking secrets."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_DATABASE_PATH = Path("data/telegram-ping.sqlite3")
DEFAULT_LINK_TTL_SECONDS = 900
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
DEFAULT_API_BASE_URL = "https://api.telegram.org"
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_BOT_ID = re.compile(r"[0-9]+\Z")
_NEGATIVE_INTEGER = re.compile(r"-[0-9]+\Z")
_DECIMAL_INTEGER = re.compile(r"[0-9]+\Z")


class ConfigError(ValueError):
    """A safe configuration error that never includes environment values."""


def normalize_api_base_url(value: str) -> str:
    """Permit Telegram's cloud endpoint or an explicitly configured loopback API."""
    error = "TELEGRAM_API_BASE_URL must be the Telegram cloud URL or loopback HTTP."
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ConfigError(error)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise ConfigError(error) from None
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not hostname
        or "%" in hostname
    ):
        raise ConfigError(error)
    if parsed.scheme == "https" and hostname == "api.telegram.org" and port in {None, 443}:
        return DEFAULT_API_BASE_URL
    if parsed.scheme != "http" or (port is not None and not 1 <= port <= 65535):
        raise ConfigError(error)
    if hostname.lower() == "localhost":
        hostname = "127.0.0.1"  # Resolve no operator-controlled DNS names for local API access.
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        raise ConfigError(error) from None
    if not address.is_loopback:
        raise ConfigError(error)
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"http://{host}" + (f":{port}" if port is not None else "")


def _parse_attachment_limit(value: str) -> int:
    if not value:
        return MAX_ATTACHMENT_BYTES
    if len(value) > 9 or _DECIMAL_INTEGER.fullmatch(value) is None:
        raise ConfigError("MAX_ATTACHMENT_BYTES must be between 1 and 104857600.")
    result = int(value)
    if not 1 <= result <= MAX_ATTACHMENT_BYTES:
        raise ConfigError("MAX_ATTACHMENT_BYTES must be between 1 and 104857600.")
    return result


def _parse_local_files_root(value: str, api_base_url: str) -> Path | None:
    if not value:
        return None
    if "\x00" in value or not Path(value).is_absolute():
        raise ConfigError("TELEGRAM_LOCAL_FILES_ROOT must be an absolute directory path.")
    if api_base_url == DEFAULT_API_BASE_URL:
        raise ConfigError("TELEGRAM_LOCAL_FILES_ROOT requires a local Bot API endpoint.")
    return Path(value)


def _setting(environ: Mapping[str, str], name: str, default: str = "") -> str:
    value = environ.get(name, default)
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be text.")
    return value


def _parse_bot_token(value: str) -> str:
    if value == "":
        return ""
    bot_id, separator, secret = value.partition(":")
    if (
        not separator
        or _BOT_ID.fullmatch(bot_id) is None
        or re.fullmatch(r"[A-Za-z0-9_-]+", secret) is None
    ):
        raise ConfigError("TELEGRAM_BOT_TOKEN must be a numeric ID, a colon, and a token.")
    return value


def _parse_link_ttl(value: str) -> int:
    if not value:
        return DEFAULT_LINK_TTL_SECONDS
    if len(value.strip()) > 5 or _DECIMAL_INTEGER.fullmatch(value.strip()) is None:
        raise ConfigError("LINK_TTL_SECONDS must be an integer between 60 and 86400.")
    seconds = int(value)
    if not 60 <= seconds <= 86400:
        raise ConfigError("LINK_TTL_SECONDS must be an integer between 60 and 86400.")
    return seconds


def _parse_group_chat_ids(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()

    chat_ids: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if len(item) > 20 or _NEGATIVE_INTEGER.fullmatch(item) is None:
            raise ConfigError("ALLOWED_GROUP_CHAT_IDS must be comma-separated negative integers.")
        chat_id = int(item)
        if not -(2**63 - 1) <= chat_id <= -1:
            raise ConfigError("ALLOWED_GROUP_CHAT_IDS must contain only negative integers.")
        chat_ids.add(chat_id)
    return frozenset(chat_ids)


def _parse_port(value: str) -> str:
    if value == "*":
        return value
    if len(value) > 5 or _DECIMAL_INTEGER.fullmatch(value) is None:
        raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")
    return str(port)


def _normalize_allowed_host(value: str) -> str:
    if not value or any(character.isspace() for character in value):
        raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")

    if value.startswith("["):
        close_bracket = value.find("]")
        if close_bracket < 0:
            raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")
        literal = value[1:close_bracket]
        suffix = value[close_bracket + 1 :]
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:
            raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.") from None
        if address.version != 6 or address.is_unspecified:
            raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")
        if suffix and not suffix.startswith(":"):
            raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")
        port = _parse_port(suffix[1:]) if suffix else "*"
        return f"[{address.compressed}]:{port}"

    if value.count(":") > 1:
        raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")
    if ":" in value:
        hostname, port_value = value.rsplit(":", 1)
        port = _parse_port(port_value)
    else:
        hostname, port = value, "*"
    if not hostname:
        raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        hostname = hostname.rstrip(".").lower()
        if (
            not hostname
            or len(hostname) > 253
            or any(_HOST_LABEL.fullmatch(label) is None for label in hostname.split("."))
            or all(label.isdecimal() for label in hostname.split("."))
        ):
            raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.") from None
        return f"{hostname}:{port}"

    if address.version != 4 or address.is_unspecified:
        raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")
    return f"{address.compressed}:{port}"


def _parse_allowed_hosts(value: str) -> tuple[str, ...]:
    if not value.strip():
        return DEFAULT_ALLOWED_HOSTS
    hosts = [item.strip() for item in value.split(",")]
    if any(not item for item in hosts):
        raise ConfigError("MCP_ALLOWED_HOSTS must be a comma-separated list of hosts.")
    return tuple(dict.fromkeys(_normalize_allowed_host(item) for item in hosts))


def _parse_polling_enabled(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ConfigError("POLLING_ENABLED must be true, false, 1, or 0.")


def _validate_bind_host(host: str) -> None:
    if not host or any(character.isspace() for character in host):
        raise ConfigError("The server host must be a valid IP address or hostname.")
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
        return
    except ValueError:
        pass

    hostname = host.rstrip(".").lower()
    if (
        not hostname
        or len(hostname) > 253
        or any(_HOST_LABEL.fullmatch(label) is None for label in hostname.split("."))
        or all(label.isdecimal() for label in hostname.split("."))
    ):
        raise ConfigError("The server host must be a valid IP address or hostname.")


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable service configuration parsed from environment variables."""

    bot_token: str = field(default="", repr=False)
    auth_token: str = field(default="", repr=False)
    database_path: Path = DEFAULT_DATABASE_PATH
    link_ttl_seconds: int = DEFAULT_LINK_TTL_SECONDS
    allowed_group_chat_ids: frozenset[int] = frozenset()
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    polling_enabled: bool = True
    api_base_url: str = DEFAULT_API_BASE_URL
    local_files_root: Path | None = None
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        """Read and validate the supported settings without echoing their values."""

        source = os.environ if environ is None else environ
        bot_token = _parse_bot_token(_setting(source, "TELEGRAM_BOT_TOKEN"))
        auth_token = _setting(source, "MCP_AUTH_TOKEN")

        database_value = _setting(source, "DATABASE_PATH")
        if "\x00" in database_value:
            raise ConfigError("DATABASE_PATH must be a valid filesystem path.")
        database_path = Path(database_value) if database_value else DEFAULT_DATABASE_PATH
        api_base_url = normalize_api_base_url(
            _setting(source, "TELEGRAM_API_BASE_URL", DEFAULT_API_BASE_URL)
        )

        return cls(
            bot_token=bot_token,
            auth_token=auth_token,
            database_path=database_path,
            link_ttl_seconds=_parse_link_ttl(_setting(source, "LINK_TTL_SECONDS")),
            allowed_group_chat_ids=_parse_group_chat_ids(
                _setting(source, "ALLOWED_GROUP_CHAT_IDS")
            ),
            allowed_hosts=_parse_allowed_hosts(_setting(source, "MCP_ALLOWED_HOSTS")),
            polling_enabled=_parse_polling_enabled(_setting(source, "POLLING_ENABLED", "true")),
            api_base_url=api_base_url,
            local_files_root=_parse_local_files_root(
                _setting(source, "TELEGRAM_LOCAL_FILES_ROOT"), api_base_url
            ),
            max_attachment_bytes=_parse_attachment_limit(_setting(source, "MAX_ATTACHMENT_BYTES")),
        )

    def validate_for_serve(self, transport: str, host: str) -> None:
        """Validate credentials and host policy needed by the requested server mode."""

        if not self.bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required to serve.")
        normalized_api = normalize_api_base_url(self.api_base_url)
        _parse_local_files_root(str(self.local_files_root or ""), normalized_api)
        if (
            isinstance(self.max_attachment_bytes, bool)
            or not isinstance(self.max_attachment_bytes, int)
            or not 1 <= self.max_attachment_bytes <= MAX_ATTACHMENT_BYTES
        ):
            raise ConfigError("MAX_ATTACHMENT_BYTES must be between 1 and 104857600.")
        if transport not in {"stdio", "http"}:
            raise ConfigError("Transport must be either 'stdio' or 'http'.")
        if not isinstance(host, str):
            raise ConfigError("The server host must be a valid IP address or hostname.")
        _validate_bind_host(host)
        if transport == "stdio":
            return
        if len(self.auth_token) < 32:
            raise ConfigError("MCP_AUTH_TOKEN must contain at least 32 characters for HTTP.")
        if any(not 33 <= ord(character) <= 126 for character in self.auth_token):
            raise ConfigError("MCP_AUTH_TOKEN must contain printable ASCII without spaces.")

    def diagnostic(self) -> dict[str, bool]:
        """Return safe configuration status flags for the offline doctor command."""

        return {
            "token_configured": bool(self.bot_token),
            "auth_token_configured": bool(self.auth_token),
            "http_auth_valid": len(self.auth_token) >= 32
            and all(33 <= ord(character) <= 126 for character in self.auth_token),
            "database_configured": bool(self.database_path),
            "link_ttl_valid": 60 <= self.link_ttl_seconds <= 86400,
            "allowed_groups_configured": bool(self.allowed_group_chat_ids),
            "allowed_hosts_configured": bool(self.allowed_hosts),
            "polling_enabled": self.polling_enabled,
        }
