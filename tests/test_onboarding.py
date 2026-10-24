from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from telegram_ping_mcp.config import Config, ConfigError
from telegram_ping_mcp.onboarding import command_menu, onboarding_text
from telegram_ping_mcp.server import Runtime
from telegram_ping_mcp.service import Service
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramTransportError

PUBLIC_URL = "https://connector.example.test/mcp"


class FakeBot:
    def __init__(
        self, *, failure: Exception | None = None, menu_failure: Exception | None = None
    ) -> None:
        self.sends: list[dict] = []
        self.command_menus: list[dict] = []
        self.failure = failure
        self.menu_failure = menu_failure

    async def get_me(self) -> dict:
        return {"username": "synthetic_bot"}

    async def send_message(self, chat_id: int, text: str, **kwargs) -> dict:
        self.sends.append({"chat_id": chat_id, "text": text, **kwargs})
        if self.failure is not None:
            raise self.failure
        return {"message_id": len(self.sends)}

    async def set_commands(self, commands: list[dict], *, chat_id: int | None = None) -> None:
        self.command_menus.append({"commands": copy.deepcopy(commands), "chat_id": chat_id})
        if self.menu_failure is not None:
            raise self.menu_failure

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
    assert "/help" not in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096


def test_menus_offer_only_commands_for_the_binding_and_do_not_share_mutable_entries() -> None:
    assert command_menu() == [{"command": "connect", "description": "Как подключить AI-чат"}]
    assert [entry["command"] for entry in command_menu(linked=True)] == [
        "start",
        "connect",
        "stop",
        "unlink",
    ]
    menu = command_menu()
    menu[0]["command"] = "changed"
    assert command_menu()[0]["command"] == "connect"


def assert_intro(text: str, *, linked: bool) -> None:
    assert "AI Connector" in text
    assert "уведомления и сообщения" in text
    assert "читать ваши ответы и файлы" in text
    assert "Модель выбирается в AI-приложении" in text
    assert "/connect" in text
    assert ("/stop" in text) is linked
    assert ("/unlink" in text) is linked
    assert "https://" not in text
    assert len(text) < 500


async def test_start_is_short_and_keeps_only_relevant_binding_status(setup) -> None:
    service, store, bot = setup
    await service.handle_update(command("/start"))
    assert bot.sends[-1]["text"].startswith("AI Connector")
    assert "администратор" not in bot.sends[-1]["text"]
    assert "ссылку-приглашение" not in bot.sends[-1]["text"]
    assert service.find_users() == []
    assert_intro(bot.sends[-1]["text"], linked=False)
    assert bot.command_menus[-1] == {"commands": command_menu(), "chat_id": 7001}

    await service.handle_update(command("/start synthetic-invalid-link"))
    assert bot.sends[-1]["text"].startswith("Ссылка недействительна или устарела.")
    assert "synthetic-invalid-link" not in bot.sends[-1]["text"]
    assert_intro(bot.sends[-1]["text"], linked=False)
    assert bot.command_menus[-1]["commands"] == command_menu()

    link = service.create_user_link("synthetic", "Synthetic")
    token = parse_qs(urlsplit(link["link"]).query)["start"][0]
    await service.handle_update(command(f"/start {token}"))
    assert bot.sends[-1]["text"].splitlines()[0] == "Связь подтверждена. Уведомления включены."
    assert_intro(bot.sends[-1]["text"], linked=True)
    assert bot.command_menus[-1]["commands"] == command_menu(linked=True)
    assert service.get_user("synthetic")["active"] is True

    await service.handle_update(command(f"/start {token}"))
    assert bot.sends[-1]["text"].startswith("Ссылка недействительна или устарела.")
    assert token not in bot.sends[-1]["text"]
    assert_intro(bot.sends[-1]["text"], linked=True)

    store.set_active_by_telegram(7001, False)
    await service.handle_update(command("/start"))
    assert bot.sends[-1]["text"].splitlines()[0] == "Уведомления включены."
    assert service.get_user("synthetic")["active"] is True
    assert_intro(bot.sends[-1]["text"], linked=True)
    await service.handle_update(command("/start"))
    assert bot.sends[-1]["text"].splitlines()[0] == "Уведомления включены."
    assert_intro(bot.sends[-1]["text"], linked=True)
    assert all("parse_mode" not in sent for sent in bot.sends)


@pytest.mark.parametrize("active", [None, False, True])
@pytest.mark.parametrize("text", ["/connect", "/connect@SYNTHETIC_BOT"])
async def test_connect_is_available_without_changing_binding_or_optin(setup, active, text) -> None:
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
    assert bot.command_menus == [
        {"commands": command_menu(linked=active is not None), "chat_id": 7001}
    ]


async def test_connect_reply_failure_does_not_change_optin(tmp_path) -> None:
    store = Store(tmp_path / "blocked.sqlite3")
    bind(store)
    bot = FakeBot(failure=TelegramAPIError(403))
    service = Service(store, bot, "synthetic_bot")
    before = service.get_user("synthetic")
    await service.handle_update(command("/connect"))
    assert service.get_user("synthetic") == before
    assert len(bot.sends) == 1
    assert bot.command_menus[-1]["commands"] == command_menu(linked=True)


@pytest.mark.parametrize(
    "text",
    [
        "/connect@other_bot",
        "/connect token",
        "/connect ",
        "/connect\n",
        "/CONNECT",
        "/help",
        "Обычный текст",
    ],
)
async def test_connect_ignores_other_bots_arguments_and_ordinary_text(setup, text) -> None:
    service, _, bot = setup
    await service.handle_update(command(text))
    assert bot.sends == []
    assert bot.command_menus == []
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
async def test_untrusted_connect_updates_cannot_trigger_replies_or_menus(setup, mutation) -> None:
    service, _, bot = setup
    update = command("/connect")
    update["message"].update(copy.deepcopy(mutation))
    await service.handle_update(update)
    await service.handle_update({"edited_message": command("/connect")["message"]})
    assert bot.sends == []
    assert bot.command_menus == []


@pytest.mark.parametrize("text", ["/stop", "/unlink"])
async def test_optout_commands_remain_concise(setup, text) -> None:
    service, store, bot = setup
    bind(store)
    await service.handle_update(command(text))
    assert len(bot.sends) == 1
    assert "AI Connector" not in bot.sends[0]["text"]
    assert len(bot.sends[0]["text"]) < 150
    assert service.get_user("synthetic") is None or not service.get_user("synthetic")["active"]
    assert bot.command_menus[-1]["commands"] == command_menu(linked=text != "/unlink")


@pytest.mark.parametrize("text", ["/stop", "/unlink"])
async def test_unlinked_management_commands_only_point_to_connect_and_do_not_mutate(setup, text):
    service, _, bot = setup
    link = service.create_user_link("synthetic", "Synthetic")
    token = parse_qs(urlsplit(link["link"]).query)["start"][0]
    await service.handle_update(command(text))
    assert bot.sends == [{"chat_id": 7001, "text": "Для подключения используйте /connect."}]
    assert bot.command_menus[-1]["commands"] == command_menu()
    assert service.find_users() == []
    await service.handle_update(command(f"/start {token}"))
    assert service.get_user("synthetic")["active"] is True


@pytest.mark.parametrize("failure", [TelegramAPIError(403), TelegramTransportError()])
async def test_menu_failure_does_not_undo_binding_and_later_command_recovers(
    tmp_path, caplog, failure
) -> None:
    store = Store(tmp_path / "menu-failure.sqlite3")
    bot = FakeBot(menu_failure=failure)
    service = Service(store, bot, "synthetic_bot")
    link = service.create_user_link("synthetic", "Synthetic")
    token = parse_qs(urlsplit(link["link"]).query)["start"][0]
    await service.handle_update(command(f"/start {token}"))
    assert service.get_user("synthetic")["active"] is True
    assert len(bot.sends) == len(bot.command_menus) == 1
    assert caplog.records[-1].message == "Telegram chat command menu could not be updated."
    bot.menu_failure = None
    await service.handle_update(command("/stop"))
    assert service.get_user("synthetic")["active"] is False
    assert bot.command_menus[-1]["commands"] == command_menu(linked=True)


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
        assert bot.command_menus == [{"commands": command_menu(), "chat_id": None}]
        await runtime.handle_update(command("/connect"))
    text = bot.sends[0]["text"]
    assert_guide(text)
    assert config.bot_token not in text
    assert config.auth_token not in text
    assert "synthetic-bot-secret" not in text


@pytest.mark.parametrize("failure", [TelegramAPIError(429), TelegramTransportError()])
async def test_runtime_menu_failure_leaves_ready_and_chat_commands_can_recover(
    tmp_path, caplog, failure
) -> None:
    config = Config(database_path=tmp_path / "runtime-menu.sqlite3", polling_enabled=False)
    bot = FakeBot(menu_failure=failure)
    runtime = Runtime(config, bot=bot)
    async with runtime.lifespan():
        assert runtime.readiness() == {"status": "ready", "polling": "disabled"}
        assert len(bot.command_menus) == 1
        assert caplog.records[-1].message == "Telegram default command menu could not be updated."
        bot.menu_failure = None
        await runtime.handle_update(command("/connect"))
        assert bot.command_menus[-1] == {"commands": command_menu(), "chat_id": 7001}


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
async def test_longest_public_url_and_connect_guide_fit_one_telegram_message(
    tmp_path, suffix
) -> None:
    prefix = "https://example.test/"
    url = prefix + suffix
    assert len(url.encode("utf-16-le")) // 2 == 2048
    bot = FakeBot()
    service = Service(Store(tmp_path / "length.sqlite3"), bot, "synthetic_bot", public_mcp_url=url)
    await service.handle_update(command("/connect"))
    assert url in bot.sends[0]["text"]
    assert len(bot.sends[0]["text"].encode("utf-16-le")) // 2 <= 4096
