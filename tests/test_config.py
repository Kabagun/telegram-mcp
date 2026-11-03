import traceback
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from telegram_ping_mcp.config import (
    DEFAULT_ALLOWED_HOSTS,
    DEFAULT_API_BASE_URL,
    DEFAULT_DATABASE_PATH,
    DEFAULT_LINK_TTL_SECONDS,
    MAX_ATTACHMENT_BYTES,
    Config,
    ConfigError,
)


def test_from_env_uses_safe_defaults() -> None:
    config = Config.from_env({})

    assert config.bot_token == ""
    assert config.auth_token == ""
    assert config.database_path == DEFAULT_DATABASE_PATH
    assert config.link_ttl_seconds == DEFAULT_LINK_TTL_SECONDS
    assert config.allowed_group_chat_ids == frozenset()
    assert config.allowed_hosts == DEFAULT_ALLOWED_HOSTS
    assert config.polling_enabled is True
    assert config.api_base_url == DEFAULT_API_BASE_URL
    assert config.local_files_root is None
    assert config.max_attachment_bytes == 100 * 1024 * 1024
    assert config.telegram_api_id is None
    assert config.telegram_api_hash == ""
    assert config.diagnostic()["telegram_history_configured"] is False


def test_from_env_parses_supported_values() -> None:
    config = Config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123456:fake-test-token",
            "MCP_AUTH_TOKEN": "local-test-auth-token-value-long-enough",
            "DATABASE_PATH": "custom/state.sqlite3",
            "LINK_TTL_SECONDS": "3600",
            "ALLOWED_GROUP_CHAT_IDS": "-100101,-202,-100101",
            "MCP_ALLOWED_HOSTS": "Example.test,localhost:8123,[::1]:9000,127.0.0.1:*",
            "POLLING_ENABLED": "0",
        }
    )

    assert config.bot_token == "123456:fake-test-token"
    assert config.auth_token == "local-test-auth-token-value-long-enough"
    assert config.database_path == Path("custom/state.sqlite3")
    assert config.link_ttl_seconds == 3600
    assert config.allowed_group_chat_ids == frozenset({-100101, -202})
    assert config.allowed_hosts == (
        "example.test:*",
        "localhost:8123",
        "[::1]:9000",
        "127.0.0.1:*",
    )
    assert config.polling_enabled is False


@pytest.mark.parametrize("value", ["59", "86401", "-1", "1.5", "nope"])
def test_link_ttl_rejects_values_outside_range(value: str) -> None:
    with pytest.raises(ConfigError, match="LINK_TTL_SECONDS"):
        Config.from_env({"LINK_TTL_SECONDS": value})


@pytest.mark.parametrize(
    "value",
    ["101", "-1,0", "-1,abc", "-1,, -2", "-1, +2"],
)
def test_group_chat_ids_require_negative_integers(value: str) -> None:
    with pytest.raises(ConfigError, match="ALLOWED_GROUP_CHAT_IDS"):
        Config.from_env({"ALLOWED_GROUP_CHAT_IDS": value})


@pytest.mark.parametrize("value", ["yes", "on", "2", ""])
def test_polling_enabled_requires_a_strict_boolean(value: str) -> None:
    with pytest.raises(ConfigError, match="POLLING_ENABLED"):
        Config.from_env({"POLLING_ENABLED": value})


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "https://example.test",
        "example.test/path",
        "user:password@example.test",
        "example.test:0",
        "example.test:65536",
        "[::1",
        "::1",
        "0.0.0.0",
    ],
)
def test_allowed_hosts_reject_unsafe_or_malformed_entries(value: str) -> None:
    with pytest.raises(ConfigError, match="MCP_ALLOWED_HOSTS") as error:
        Config.from_env({"MCP_ALLOWED_HOSTS": value})

    assert value not in str(error.value)


@pytest.mark.parametrize(
    "value",
    ["bad-token", ":missing-id", "123456:", "123456:contains space", "12x:opaque"],
)
def test_bot_token_rejects_malformed_values_without_echoing_them(value: str) -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN") as error:
        Config.from_env({"TELEGRAM_BOT_TOKEN": value})

    assert value not in str(error.value)


def test_validate_for_serve_requires_bot_token_for_each_transport() -> None:
    config = Config()

    for transport in ("stdio", "http"):
        with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
            config.validate_for_serve(transport, "127.0.0.1")


def test_validate_for_serve_requires_strong_http_auth_token() -> None:
    config = Config(bot_token="123456:fake-test-token", auth_token="too-short")

    with pytest.raises(ConfigError, match="MCP_AUTH_TOKEN"):
        config.validate_for_serve("http", "0.0.0.0")


def test_validate_for_serve_accepts_http_credentials_and_valid_bind_hosts() -> None:
    config = Config(
        bot_token="123456:fake-test-token",
        auth_token="x" * 32,
    )

    config.validate_for_serve("http", "0.0.0.0")
    config.validate_for_serve("http", "[::]")
    config.validate_for_serve("stdio", "localhost")


def test_validate_for_serve_rejects_unknown_transport_and_invalid_host() -> None:
    config = Config(bot_token="123456:fake-test-token", auth_token="x" * 32)

    with pytest.raises(ConfigError, match="Transport"):
        config.validate_for_serve("sse", "127.0.0.1")
    with pytest.raises(ConfigError, match="server host"):
        config.validate_for_serve("http", "https://example.test")


def test_diagnostic_contains_status_only_and_never_secrets() -> None:
    bot_token = "123456:fake-test-token"
    auth_token = "a-long-fake-auth-token-for-tests-only"
    config = Config(bot_token=bot_token, auth_token=auth_token)

    status = config.diagnostic()

    assert status == {
        "token_configured": True,
        "auth_token_configured": True,
        "http_auth_valid": True,
        "database_configured": True,
        "link_ttl_valid": True,
        "allowed_groups_configured": False,
        "allowed_hosts_configured": True,
        "polling_enabled": True,
        "telegram_history_configured": False,
    }
    assert bot_token not in repr(status)
    assert auth_token not in repr(status)
    assert bot_token not in repr(config)
    assert auth_token not in repr(config)


@pytest.mark.parametrize("auth", [" " * 32, "x" * 32 + "\n", "я" * 32])
def test_http_auth_token_must_fit_an_http_header(auth):
    config = Config(bot_token="123456:fake-test-token", auth_token=auth)
    with pytest.raises(ConfigError, match="MCP_AUTH_TOKEN") as error:
        config.validate_for_serve("http", "127.0.0.1")
    assert auth not in str(error.value)
    assert config.diagnostic()["http_auth_valid"] is False


@pytest.mark.parametrize(
    "setting,value",
    [
        ("LINK_TTL_SECONDS", "9" * 5000),
        ("ALLOWED_GROUP_CHAT_IDS", "-" + "9" * 5000),
        ("ALLOWED_GROUP_CHAT_IDS", "-9223372036854775809"),
        ("MCP_ALLOWED_HOSTS", "example.invalid:" + "9" * 5000),
    ],
)
def test_environment_numbers_are_bounded_before_integer_parsing(setting, value):
    with pytest.raises(ConfigError) as error:
        Config.from_env({setting: value})
    assert value not in str(error.value)


def test_config_is_frozen() -> None:
    config = Config()

    with pytest.raises(FrozenInstanceError):
        config.bot_token = "123456:replacement-test-token"  # type: ignore[misc]


def test_local_api_settings_and_configured_size_limit(tmp_path):
    config = Config.from_env(
        {
            "TELEGRAM_API_BASE_URL": "http://localhost:8081/",
            "TELEGRAM_LOCAL_FILES_ROOT": str(tmp_path),
            "MAX_ATTACHMENT_BYTES": str(MAX_ATTACHMENT_BYTES),
        }
    )
    assert config.api_base_url == "http://127.0.0.1:8081"
    assert config.local_files_root == tmp_path
    assert config.max_attachment_bytes == MAX_ATTACHMENT_BYTES


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com",
        "http://example.com",
        "http://0.0.0.0:8081",
        "http://192.168.0.1:8081",
        "http://169.254.169.254",
        "https://127.0.0.1:8081",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:8081/path",
        "http://name:secret@127.0.0.1:8081",
        "http://127.0.0.1?secret=credential",
        "http://127.0.0.1#secret",
        "http://127.0.0.1\n",
        "file:///tmp/bot-api",
        "http://[::1%25eth0]:8081",
        "http://127.0.0.1.example.com",
        "",
    ],
)
def test_api_endpoints_cannot_target_arbitrary_hosts(endpoint):
    with pytest.raises(ConfigError) as caught:
        Config.from_env({"TELEGRAM_API_BASE_URL": endpoint})
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:8081", "http://[::1]:8081"])
def test_loopback_ipv4_and_ipv6_api_endpoints_are_supported(endpoint):
    assert Config.from_env({"TELEGRAM_API_BASE_URL": endpoint}).api_base_url == endpoint


@pytest.mark.parametrize("limit", ["0", "-1", "104857601", "x", "9" * 5000, "1.5"])
def test_attachment_limit_is_bounded_before_integer_conversion(limit):
    with pytest.raises(ConfigError, match="MAX_ATTACHMENT_BYTES"):
        Config.from_env({"MAX_ATTACHMENT_BYTES": limit})


def test_local_files_root_requires_local_endpoint_and_absolute_path(tmp_path):
    with pytest.raises(ConfigError, match="local Bot API"):
        Config.from_env({"TELEGRAM_LOCAL_FILES_ROOT": str(tmp_path)})
    with pytest.raises(ConfigError, match="absolute directory"):
        Config.from_env(
            {
                "TELEGRAM_API_BASE_URL": "http://127.0.0.1:8081",
                "TELEGRAM_LOCAL_FILES_ROOT": "relative/files",
            }
        )


@pytest.mark.parametrize("api_id", ["1", "0042", "2147483647"])
@pytest.mark.parametrize("api_hash", ["a" * 32, "0123456789abcdefABCDEF0123456789"])
def test_optional_bot_history_credentials_parse_and_validate(api_id, api_hash):
    config = Config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123456:synthetic-test-token",
            "TELEGRAM_API_ID": api_id,
            "TELEGRAM_API_HASH": api_hash,
        }
    )
    assert config.telegram_api_id == int(api_id)
    assert config.telegram_api_hash == api_hash
    assert config.diagnostic()["telegram_history_configured"] is True
    config.validate_for_serve("stdio", "127.0.0.1")


def test_empty_bot_history_pair_disables_optional_reader():
    config = Config.from_env({"TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": ""})
    assert config.telegram_api_id is None
    assert config.telegram_api_hash == ""
    assert config.diagnostic()["telegram_history_configured"] is False


@pytest.mark.parametrize(
    "values",
    [
        {"TELEGRAM_API_ID": "7654321"},
        {"TELEGRAM_API_HASH": "b" * 32},
        {"TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": "b" * 32},
        {"TELEGRAM_API_ID": "7654321", "TELEGRAM_API_HASH": ""},
    ],
)
def test_bot_history_credentials_require_the_complete_pair(values):
    with pytest.raises(ConfigError) as caught:
        Config.from_env(values)
    assert str(caught.value) == (
        "TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured together."
    )
    output = "".join(traceback.format_exception(caught.value))
    assert all(value not in output for value in values.values() if value)


@pytest.mark.parametrize(
    "api_id",
    ["0", "-1", "+1", "1.5", " 42", "42 ", "42\n", "１２", "٤٢", "2147483648", "9" * 5000],
)
def test_bot_history_api_id_is_bounded_ascii_decimal(api_id):
    with pytest.raises(ConfigError) as caught:
        Config.from_env({"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": "a" * 32})
    assert str(caught.value) == (
        "TELEGRAM_API_ID must be a positive decimal integer up to 2147483647."
    )
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "api_hash", ["private-synthetic-value", "a" * 31, "a" * 33, "g" * 32, " " * 32]
)
def test_bot_history_api_hash_requires_exactly_32_hex_characters(api_hash):
    with pytest.raises(ConfigError) as caught:
        Config.from_env({"TELEGRAM_API_ID": "7654321", "TELEGRAM_API_HASH": api_hash})
    assert str(caught.value) == "TELEGRAM_API_HASH must contain exactly 32 hexadecimal characters."
    if api_hash.strip():
        assert api_hash not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("name", ["TELEGRAM_API_ID", "TELEGRAM_API_HASH"])
@pytest.mark.parametrize("value", [True, 42, None])
def test_bot_history_env_values_must_be_text(name, value):
    with pytest.raises(ConfigError, match=f"{name} must be text"):
        Config.from_env({name: value})


@pytest.mark.parametrize(
    "api_id,api_hash",
    [
        (True, "a" * 32),
        (False, "a" * 32),
        ("42", "a" * 32),
        (42.0, "a" * 32),
        (0, "a" * 32),
        (-1, "a" * 32),
        (2**31, "a" * 32),
        (None, "a" * 32),
        (42, ""),
        (42, None),
        (42, True),
        (42, "private-synthetic-value"),
        (42, "a" * 32 + "\n"),
    ],
)
@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_direct_config_history_credentials_are_validated_without_coercion(
    api_id, api_hash, transport
):
    config = Config(
        bot_token="123456:synthetic-test-token",
        auth_token="s" * 32,
        telegram_api_id=api_id,
        telegram_api_hash=api_hash,
    )
    with pytest.raises(ConfigError):
        config.validate_for_serve(transport, "127.0.0.1")
    assert config.diagnostic()["telegram_history_configured"] is False


def test_bot_history_credentials_are_hidden_from_repr_and_diagnostics():
    config = Config(telegram_api_id=7654321, telegram_api_hash="b" * 32)
    assert "7654321" not in repr(config)
    assert "b" * 32 not in repr(config)
    secret_fields = {item.name: item.repr for item in fields(Config)}
    assert secret_fields["telegram_api_id"] is False
    assert secret_fields["telegram_api_hash"] is False
    diagnostic = config.diagnostic()
    assert diagnostic["telegram_history_configured"] is True
    assert "telegram_api_id" not in diagnostic
    assert "telegram_api_hash" not in diagnostic
    assert all(type(value) is bool for value in diagnostic.values())
