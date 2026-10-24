from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from telegram_ping_mcp.config import Config, ConfigError
from telegram_ping_mcp.onboarding import onboarding_text
from telegram_ping_mcp.server import Runtime
from telegram_ping_mcp.service import Service
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramAPIError

PUBLIC_URL = "https://connector.example.test/mcp"


class FakeBot:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.sends: list[dict] = []
        self.failure = failure

    async def get_me(self) -> dict:
        return {"username": "synthetic_bot"}

    async def send_message(self, chat_id: int, text: str, **kwargs) -> dict:
        self.sends.append({"chat_id": chat_id, "text": text, **kwargs})
        if self.failure is not None:
            raise self.failure
        return {"message_id": len(self.sends)}

    async def aclose(self) -> None:
        pass


def command(text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"type": "private", "id": 7001},
            "from": {"id": 7001, "is_bot": False, "first_name": "Synthetic"},
            "text": text,
        },
    }


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Service, Store, FakeBot]:
    store = Store(tmp_path / "onboarding.sqlite3")
    bot = FakeBot()
    return Service(store, bot, "synthetic_bot", public_mcp_url=PUBLIC_URL), store, bot


def bind(store: Store) -> None:
    link = store.create_link("synthetic", "Synthetic")
    store.consume_link(link["token"], 7001, 7001, None, "Synthetic")


def assert_guide(text: str) -> None:
    assert "AI Connector" in text
    assert PUBLIC_URL in text
    assert "https://claude.ai/" in text
    assert "https://chatgpt.com/" in text
    assert "Streamable HTTP" in text
    assert "OAuth" in text
    assert "прямое подключение сейчас недоступно" in text
    assert "Authorization: Bearer <отдельный токен MCP>" in text
    assert "ключ API модели боту не нужен" in text
    assert "Бот сам не пробуждает AI-чат" in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096


async def test_all_start_outcomes_keep_status_and_append_guide(setup) -> None:
    service, store, bot = setup
    await service.handle_update(command("/start"))
    assert bot.sends[-1]["text"].startswith("Для подключения откройте персональную ссылку")
    assert service.find_users() == []
    assert_guide(bot.sends[-1]["text"])

    link = service.create_user_link("synthetic", "Synthetic")
    token = parse_qs(urlsplit(link["link"]).query)["start"][0]
    await service.handle_update(command(f"/start {token}"))
    assert bot.sends[-1]["text"].splitlines()[0] == "Связь подтверждена. Уведомления включены."
    assert_guide(bot.sends[-1]["text"])
    assert service.get_user("synthetic")["active"] is True

    await service.handle_update(command(f"/start {token}"))
    assert bot.sends[-1]["text"].startswith("Ссылка недействительна или устарела.")
    assert token not in bot.sends[-1]["text"]
    assert_guide(bot.sends[-1]["text"])

    store.set_active_by_telegram(7001, False)
    await service.handle_update(command("/start"))
    assert bot.sends[-1]["text"].splitlines()[0] == "Уведомления включены."
    assert service.get_user("synthetic")["active"] is True
    assert_guide(bot.sends[-1]["text"])
    await service.handle_update(command("/start"))
    assert bot.sends[-1]["text"].splitlines()[0] == "Уведомления включены."
    assert_guide(bot.sends[-1]["text"])
    assert all("parse_mode" not in sent for sent in bot.sends)


@pytest.mark.parametrize("active", [None, False, True])
@pytest.mark.parametrize("text", ["/help", "/help@SYNTHETIC_BOT"])
async def test_help_is_available_without_changing_binding_or_optin(setup, active, text) -> None:
    service, store, bot = setup
    if active is not None:
        bind(store)
        store.set_active_by_telegram(7001, active)
    before = service.find_users()
    await service.handle_update(command(text))
    assert len(bot.sends) == 1
    assert bot.sends[0]["text"] == onboarding_text(PUBLIC_URL)
    assert service.find_users() == before
    assert_guide(bot.sends[0]["text"])


async def test_help_reply_failure_does_not_change_optin(tmp_path) -> None:
    store = Store(tmp_path / "blocked.sqlite3")
    bind(store)
    bot = FakeBot(failure=TelegramAPIError(403))
    service = Service(store, bot, "synthetic_bot")
    before = service.get_user("synthetic")
    await service.handle_update(command("/help"))
    assert service.get_user("synthetic") == before
    assert len(bot.sends) == 1


@pytest.mark.parametrize(
    "text",
    ["/help@other_bot", "/help token", "/help ", "/help\n", "/HELP", "Обычный текст"],
)
async def test_help_ignores_other_bots_arguments_and_ordinary_text(setup, text) -> None:
    service, _, bot = setup
    await service.handle_update(command(text))
    assert bot.sends == []
    assert service.find_users() == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"forward_origin": {"type": "user"}},
        {"forward_date": 123},
        {"via_bot": {"id": 9999}},
        {"edit_date": 123},
        {"sender_chat": {"id": -100123}},
        {"chat": {"id": -100123, "type": "group"}},
        {"chat": {"id": 7002, "type": "private"}},
        {"from": {"id": 7001, "is_bot": True}},
        {"from": {"id": True, "is_bot": False}},
    ],
)
async def test_untrusted_help_updates_cannot_trigger_replies(setup, mutation) -> None:
    service, _, bot = setup
    update = command("/help")
    update["message"].update(copy.deepcopy(mutation))
    await service.handle_update(update)
    await service.handle_update({"edited_message": command("/help")["message"]})
    assert bot.sends == []


@pytest.mark.parametrize("text", ["/stop", "/unlink"])
async def test_optout_commands_remain_concise(setup, text) -> None:
    service, store, bot = setup
    bind(store)
    await service.handle_update(command(text))
    assert len(bot.sends) == 1
    assert "AI Connector" not in bot.sends[0]["text"]
    assert len(bot.sends[0]["text"]) < 150
    assert service.get_user("synthetic") is None or not service.get_user("synthetic")["active"]


async def test_runtime_passes_only_public_url_to_guide_and_never_credentials(tmp_path) -> None:
    config = Config.from_env(
        {
            "PUBLIC_MCP_URL": PUBLIC_URL,
            "TELEGRAM_BOT_TOKEN": "123456:synthetic-bot-secret",
            "MCP_AUTH_TOKEN": "synthetic-access-secret-0123456789",
            "POLLING_ENABLED": "false",
            "DATABASE_PATH": str(tmp_path / "runtime.sqlite3"),
        }
    )
    bot = FakeBot()
    runtime = Runtime(config, bot=bot)
    async with runtime.lifespan():
        await runtime.handle_update(command("/help"))
    text = bot.sends[0]["text"]
    assert_guide(text)
    assert config.bot_token not in text
    assert config.auth_token not in text
    assert "synthetic-bot-secret" not in text


def test_missing_public_url_uses_admin_instructions() -> None:
    assert Config.from_env({}).public_mcp_url == ""
    text = onboarding_text()
    assert "Адрес MCP и доступ получите у администратора" in text
    assert "https://connector.example.test" not in text


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/mcp",
        "https://example.test:8443/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]:8443/mcp",
        "https://пример.рф/mcp",
    ],
)
def test_public_mcp_url_accepts_absolute_https_endpoint(value) -> None:
    config = Config.from_env({"PUBLIC_MCP_URL": value})
    assert config.public_mcp_url == value
    assert value in onboarding_text(config.public_mcp_url)


@pytest.mark.parametrize(
    "value",
    [
        "http://example.test/mcp",
        "//example.test/mcp",
        "/mcp",
        "https:///mcp",
        "https://name:synthetic-secret@example.test/mcp",
        "https://example.test/mcp?synthetic-secret",
        "https://example.test/mcp?",
        "https://example.test/mcp#synthetic-secret",
        "https://example.test/mcp#",
        " https://example.test/mcp",
        "https://example.test/a b",
        "https://example.test/mcp\n",
        "https://example.test/mcp\x00",
        "https://example.test/mcp\x7f",
        "https://example.test/mcp\u202e",
        "https://example.test/mcp\ud800",
        "https://example.test:0/mcp",
        "https://example.test:65536/mcp",
        "https://example.test:bad/mcp",
        "https://example.test:/mcp",
        "https://example..test/mcp",
        "https://example.test\\invalid/mcp",
        "https://[::1%25eth0]/mcp",
        "https://example.test/" + "x" * 2048,
        "https://example.test/" + "🙂" * 1024,
    ],
)
def test_public_mcp_url_rejects_unsafe_values_without_echoing_them(value) -> None:
    with pytest.raises(ConfigError) as caught:
        Config.from_env({"PUBLIC_MCP_URL": value})
    assert str(caught.value) == (
        "PUBLIC_MCP_URL must be an absolute HTTPS URL without credentials, query or fragment."
    )
    assert "synthetic-secret" not in str(caught.value)
    with pytest.raises(ConfigError):
        Config(bot_token="123456:synthetic", public_mcp_url=value).validate_for_serve(
            "stdio", "localhost"
        )
    with pytest.raises(ConfigError):
        onboarding_text(value)


@pytest.mark.parametrize("suffix", ["x" * 2027, "🙂" * 1013 + "x"])
async def test_longest_public_url_and_start_status_fit_one_telegram_message(
    tmp_path, suffix
) -> None:
    prefix = "https://example.test/"
    url = prefix + suffix
    assert len(url.encode("utf-16-le")) // 2 == 2048
    bot = FakeBot()
    service = Service(Store(tmp_path / "length.sqlite3"), bot, "synthetic_bot", public_mcp_url=url)
    await service.handle_update(command("/start"))
    assert url in bot.sends[0]["text"]
    assert len(bot.sends[0]["text"].encode("utf-16-le")) // 2 <= 4096
