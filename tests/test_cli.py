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
    assert captured.err == ""
