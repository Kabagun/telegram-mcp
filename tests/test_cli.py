import json
import sys

import pytest

from telegram_ping_mcp import __main__ as cli
from telegram_ping_mcp.config import Config

BOT_TOKEN = "123456:fake-test-token"
AUTH_TOKEN = "fake-local-auth-token-for-cli-tests-only"


def _mock_configuration(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(cli, "load_dotenv", lambda **_kwargs: None)


def test_doctor_is_offline_and_redacts_configured_tokens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Config(bot_token=BOT_TOKEN, auth_token=AUTH_TOKEN)
    _mock_configuration(monkeypatch, config)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("doctor must not start a runtime or network listener")

    monkeypatch.setattr(cli, "Runtime", unexpected)
    monkeypatch.setattr(cli, "create_http_app", unexpected)
    monkeypatch.setattr(cli.uvicorn, "run", unexpected)

    assert cli.main(["doctor"]) == 0

    captured = capsys.readouterr()
    assert "token_configured" in captured.out
    assert "auth_token_configured" in captured.out
    assert BOT_TOKEN not in captured.out
    assert AUTH_TOKEN not in captured.out
    assert captured.err == ""


def test_http_auth_error_is_safe_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Config(bot_token=BOT_TOKEN, auth_token="short-private-test-value")
    _mock_configuration(monkeypatch, config)
    monkeypatch.setattr(
        cli,
        "Runtime",
        lambda *_args, **_kwargs: pytest.fail("invalid config must fail before runtime startup"),
    )

    assert cli.main(["serve", "--transport", "http"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Configuration error" in captured.err
    assert "MCP_AUTH_TOKEN" in captured.err
    assert BOT_TOKEN not in captured.err
    assert config.auth_token not in captured.err


def test_stdio_uses_stderr_logging_and_delegates_without_starting_bot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Config(bot_token=BOT_TOKEN)
    _mock_configuration(monkeypatch, config)
    basic_config_calls: list[dict[str, object]] = []
    delegated: list[cli.Runtime] = []

    monkeypatch.setattr(
        cli.logging,
        "basicConfig",
        lambda **kwargs: basic_config_calls.append(kwargs),
    )

    async def fake_stdio(runtime: cli.Runtime) -> None:
        delegated.append(runtime)

    monkeypatch.setattr(cli, "_stdio", fake_stdio)

    assert cli.main(["serve", "--transport", "stdio"]) == 0

    assert len(delegated) == 1
    assert delegated[0].config is config
    assert len(basic_config_calls) == 1
    assert basic_config_calls[0]["stream"] is sys.stderr
    assert capsys.readouterr().err == ""


def test_http_creates_app_and_passes_it_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Config(bot_token=BOT_TOKEN, auth_token=AUTH_TOKEN)
    _mock_configuration(monkeypatch, config)
    app = object()
    created_runtimes: list[cli.Runtime] = []
    app_runtimes: list[cli.Runtime] = []
    uvicorn_calls: list[tuple[object, dict[str, object]]] = []

    actual_runtime = cli.Runtime

    def capture_runtime(received_config: Config) -> cli.Runtime:
        runtime = actual_runtime(received_config)
        created_runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(cli, "Runtime", capture_runtime)
    monkeypatch.setattr(
        cli,
        "create_http_app",
        lambda runtime: app_runtimes.append(runtime) or app,
    )
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda received_app, **kwargs: uvicorn_calls.append((received_app, kwargs)),
    )

    assert cli.main(["serve", "--transport", "http", "--host", "127.0.0.1", "--port", "8123"]) == 0

    assert len(created_runtimes) == 1
    assert app_runtimes == created_runtimes
    assert uvicorn_calls == [
        (
            app,
            {"host": "127.0.0.1", "port": 8123, "access_log": False},
        )
    ]
    assert capsys.readouterr().err == ""


def test_help_displays_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])

    captured = capsys.readouterr()
    assert error.value.code == 0
    assert "doctor" in captured.out
    assert "serve" in captured.out
    assert "broadcast-release" in captured.out
    assert captured.err == ""


def test_release_broadcast_uses_installed_version_closes_client_and_only_prints_counts(
    tmp_path, monkeypatch, capsys
):
    config = Config(bot_token=BOT_TOKEN, database_path=tmp_path / "synthetic.sqlite3")
    _mock_configuration(monkeypatch, config)
    message = "Синтетическое обновление\n\n• Новая возможность."
    message_file = tmp_path / "synthetic-release.txt"
    message_file.write_text(message, encoding="utf-8")
    clients = []
    received = []

    class FakeClient:
        def __init__(self, token, **kwargs):
            assert token == BOT_TOKEN
            assert kwargs["api_base_url"] == config.api_base_url
            self.closed = False
            clients.append(self)

        async def aclose(self):
            self.closed = True

    async def send(store, bot, **kwargs):
        received.append({"path": store.path, "bot": bot, **kwargs})
        return {"sent": 2, "failed": 1}

    monkeypatch.setattr(cli, "TelegramClient", FakeClient)
    monkeypatch.setattr(cli, "broadcast_release", send)
    monkeypatch.setattr(cli, "installed_version", lambda name: "9.8.7")
    monkeypatch.setattr(cli, "Runtime", lambda *_a, **_kw: pytest.fail("broadcast must not poll"))

    assert (
        cli.main(["broadcast-release", "--message-file", str(message_file), "--retry-failed"]) == 0
    )

    assert received == [
        {
            "path": config.database_path,
            "bot": clients[0],
            "version": "9.8.7",
            "message": message,
            "retry_failed": True,
        }
    ]
    assert clients[0].closed
    output = capsys.readouterr()
    assert json.loads(output.out) == {"sent": 2, "failed": 1}
    assert output.err == ""


def test_release_broadcast_requires_message_file(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["broadcast-release"])
    assert error.value.code == 2
    assert "--message-file" in capsys.readouterr().err


def test_release_broadcast_file_errors_do_not_expose_paths_or_credentials(
    tmp_path, monkeypatch, capsys
):
    _mock_configuration(monkeypatch, Config(bot_token=BOT_TOKEN))
    missing = tmp_path / "synthetic-private-message-path.txt"
    monkeypatch.setattr(
        cli, "TelegramClient", lambda *_a, **_kw: pytest.fail("missing input must fail offline")
    )

    assert cli.main(["broadcast-release", "--message-file", str(missing)]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Release broadcast failed.\n"
    assert BOT_TOKEN not in output.err
    assert str(missing) not in output.err


def test_release_broadcast_closes_client_and_redacts_unexpected_failure(
    tmp_path, monkeypatch, capsys
):
    _mock_configuration(
        monkeypatch, Config(bot_token=BOT_TOKEN, database_path=tmp_path / "synthetic.sqlite3")
    )
    message_file = tmp_path / "synthetic-release.txt"
    message_file.write_text("Synthetic release", encoding="utf-8")
    closed = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def aclose(self):
            closed.append(True)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-content-and-token")

    monkeypatch.setattr(cli, "TelegramClient", FakeClient)
    monkeypatch.setattr(cli, "broadcast_release", fail)
    monkeypatch.setattr(cli, "installed_version", lambda name: "9.8.7")

    assert cli.main(["broadcast-release", "--message-file", str(message_file)]) == 1

    assert closed == [True]
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Release broadcast failed.\n"
