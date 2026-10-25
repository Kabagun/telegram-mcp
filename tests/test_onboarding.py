from __future__ import annotations

import copy
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from telegram_ping_mcp.config import Config, ConfigError
from telegram_ping_mcp.onboarding import (
    BUTTON_ACTIONS,
    CALLBACK_ACTIONS,
    client_picker,
    command_menu,
    help_presentation,
    menu_keyboard,
    onboarding_presentation,
    onboarding_text,
    privacy_presentation,
)
from telegram_ping_mcp.server import Runtime
from telegram_ping_mcp.service import Service, ServiceError
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramTransportError

PUBLIC_URL = "https://connector.example.test/mcp"


class FakeBot:
    def __init__(
        self, *, failure: Exception | None = None, menu_failure: Exception | None = None
    ) -> None:
        self.sends: list[dict] = []
        self.documents: list[dict] = []
        self.command_menus: list[dict] = []
        self.acknowledgements: list[str] = []
        self.failure = failure
        self.menu_failure = menu_failure

    async def get_me(self) -> dict:
        return {"username": "synthetic_bot"}

    async def send_message(self, chat_id: int, text: str, **kwargs) -> dict:
        self.sends.append({"chat_id": chat_id, "text": text, **kwargs})
        if self.failure is not None:
            raise self.failure
        return {"message_id": len(self.sends), "chat": {"id": chat_id}}

    async def send_connector_icon(self, chat_id: int, **kwargs) -> dict:
        self.documents.append({"chat_id": chat_id, **kwargs})
        return {"message_id": len(self.sends) + len(self.documents), "chat": {"id": chat_id}}

    async def set_commands(self, commands: list[dict], *, chat_id: int | None = None) -> None:
        self.command_menus.append({"commands": copy.deepcopy(commands), "chat_id": chat_id})
        if self.menu_failure is not None:
            raise self.menu_failure

    async def answer_callback_query(self, query_id: str) -> None:
        self.acknowledgements.append(query_id)

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


def callback(action: str, *, telegram_id: int = 7001, query_id: str = "synthetic-query") -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": query_id,
            "from": {"id": telegram_id, "is_bot": False, "first_name": "Synthetic"},
            "message": {
                "message_id": 17,
                "date": 1,
                "chat": {"type": "private", "id": telegram_id},
                "from": {"id": 9999, "is_bot": True, "username": "synthetic_bot"},
            },
            "data": action,
        },
    }


def button(action: str) -> str:
    return next(label for label, target in BUTTON_ACTIONS.items() if target == action)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Service, Store, FakeBot]:
    store = Store(tmp_path / "onboarding.sqlite3")
    bot = FakeBot()
    return Service(store, bot, "synthetic_bot", public_mcp_url=PUBLIC_URL), store, bot


def bind(store: Store) -> None:
    link = store.create_link("synthetic", "Synthetic")
    store.consume_link(link["token"], 7001, 7001, None, "Synthetic")


def assert_guide(text: str, *, linked: bool = False) -> None:
    assert text.startswith("Подключение к ChatGPT\n\nНастройки плагина\n")
    assert f"URL-адрес сервера: {PUBLIC_URL}\n" in text
    assert "Название: Telegram\n" in text
    assert "📎 Иконка PNG — файлом ниже." in text
    assert "Аутентификация: OAuth" in text
    assert "Вставьте код в окне подключения." in text
    assert "приглашение" not in text
    assert text.count("https://") == 1
    for removed in (
        "DCR",
        "Scope",
        "Client ID",
        "Client Secret",
        "OIDC",
        "Описание:",
        "Claude",
        "Доступ и приватность",
        "/unlink",
        "Streamable",
        "Bearer",
        "Authorization",
        "PKCE",
        "API",
        "токен",
        "недоступно",
        "support.claude.com",
        "developers.openai.com",
        "learn.chatgpt.com",
    ):
        assert removed not in text
    assert "/help" not in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096


def test_guide_explains_bot_chat_access_and_user_consent_without_absolute_security_claims() -> None:
    text, keyboard = privacy_presentation()
    assert "Плагин работает только с вашим чатом с ботом" in text
    assert "не входит в Telegram-аккаунт" in text
    assert "не видит остальные переписки, контакты и группы" in text
    assert "может читать сообщения и файлы, которые вы отправляете этому боту" in text
    assert "и присылать вам ответы" in text
    assert "Другие пользователи не видят ваши данные" in text
    assert "Передача защищена HTTPS, доступ подтверждаете вы" in text
    assert "🔗 Отключить" in text
    assert "/unlink" not in text
    assert keyboard is None
    for claim in ("сквозное шифрование", "end-to-end", "полностью безопас", "не имеет доступа"):
        assert claim not in text


@pytest.mark.parametrize(
    ("endpoint", "icon_url"),
    [
        ("https://example.test/nested/path/mcp", "https://example.test/assets/telegram-icon.png"),
        (
            "https://example.test:8443/nested/mcp",
            "https://example.test:8443/assets/telegram-icon.png",
        ),
        ("https://[::1]:8443/mcp", "https://[::1]:8443/assets/telegram-icon.png"),
        ("https://пример.рф/mcp", "https://xn--e1afmkfd.xn--p1ai/assets/telegram-icon.png"),
    ],
)
def test_icon_download_uses_public_endpoint_origin(endpoint, icon_url) -> None:
    text = onboarding_text(endpoint, linked=True)
    assert f"URL-адрес сервера: {endpoint}\n" in text
    _, keyboard = onboarding_presentation(text)
    assert all("url" not in item for row in keyboard["inline_keyboard"] for item in row)
    _, instructions = help_presentation("chatgpt", endpoint)
    assert {"text": "Скачать PNG", "url": icon_url} in instructions["inline_keyboard"][-1]
    assert "📎 Иконка PNG — файлом ниже." in text
    assert icon_url not in text
    assert text.count(endpoint) == 1


def entity_text(text: str, entity: dict) -> str:
    start = entity["offset"] * 2
    end = start + entity["length"] * 2
    return text.encode("utf-16-le")[start:end].decode("utf-16-le")


def copy_buttons(keyboard: dict) -> dict[str, str]:
    return {
        button["text"]: button["copy_text"]["text"]
        for row in keyboard["inline_keyboard"]
        for button in row
        if "copy_text" in button
    }


def assert_code_placement(text: str, code: str) -> None:
    sections = (
        "Название: Telegram\n",
        "URL-адрес сервера: ",
        "Аутентификация: OAuth\n",
        "🔑 Код подключения\n",
        f"Код входа: {code}\n",
        "Вставьте код в окне подключения.\n",
        "📎 Иконка PNG — файлом ниже.",
    )
    offsets = [text.index(section) for section in sections]
    assert offsets == sorted(offsets)
    assert "Аутентификация: OAuth\n\n🔑 Код подключения\n" in text
    assert f"Код входа: {code}\nБез срока действия · одноразовый.\n" in text
    assert "10 минут" not in text


@pytest.mark.parametrize("code", ["synthetic-🙂-<&_`>", "synthetic\u2028🙂\u2029code"])
def test_presentation_makes_exact_values_copyable_with_utf16_offsets_after_emoji(code) -> None:
    endpoint = "https://пример.рф/подключение/🙂/mcp"
    text = onboarding_text(endpoint, linked=True, login_code=code)
    assert_code_placement(text, code)
    entities, keyboard = onboarding_presentation(text)
    assert keyboard is not None
    expected = {
        "📋 Название": "Telegram",
        "📋 Адрес": endpoint,
        "📋 Код": code,
    }
    assert copy_buttons(keyboard) == expected
    assert {entity_text(text, item) for item in entities if item["type"] == "code"} == set(
        expected.values()
    )
    assert [entity_text(text, item) for item in entities if item["type"] == "bold"] == [
        "Подключение к ChatGPT",
        "Настройки плагина",
        "🔑 Код подключения",
    ]
    for value in (endpoint, code):
        item = next(item for item in entities if entity_text(text, item) == value)
        assert item["offset"] == len(text[: text.index(value)].encode("utf-16-le")) // 2
        assert item["length"] == len(value.encode("utf-16-le")) // 2
    previous_end = 0
    for item in entities:
        assert item["offset"] >= previous_end
        previous_end = item["offset"] + item["length"]
    assert len(keyboard["inline_keyboard"]) <= 4
    assert all(1 <= len(row) <= 3 for row in keyboard["inline_keyboard"])
    assert all(item["type"] in {"bold", "code"} for item in entities)


@pytest.mark.parametrize("linked", [False, True])
def test_presentation_without_a_login_code_has_no_code_button(linked) -> None:
    text = onboarding_text(PUBLIC_URL, linked=linked)
    assert "🔑 Код подключения" not in text
    assert "Код входа:" not in text
    _, keyboard = onboarding_presentation(text)
    assert keyboard is not None
    assert "📋 Код" not in copy_buttons(keyboard)


def test_presentation_formats_issued_codes_and_ignores_unknown_guide_lines() -> None:
    extra = (
        "\n\nПользовательский заголовок 🙂\nНазвание: Другое имя\nОписание: Другой текст\n"
        "URL-адрес сервера: http://unsafe.example.test/mcp\nКод входа: synthetic-unlinked"
    )
    text = onboarding_text(PUBLIC_URL) + extra
    entities, keyboard = onboarding_presentation(text)
    assert keyboard is not None
    assert copy_buttons(keyboard)["📋 Код"] == "synthetic-unlinked"
    extra_offset = len(text[: -len(extra)].encode("utf-16-le")) // 2
    assert all(
        item["offset"] + item["length"] <= extra_offset
        or entity_text(text, item) == "synthetic-unlinked"
        for item in entities
    )
    assert copy_buttons(keyboard)["📋 Адрес"] == PUBLIC_URL


@pytest.mark.parametrize(
    "status",
    [
        "Сначала отправьте /start, чтобы включить подключение.\n📎 Иконка PNG — файлом ниже.",
        "Уведомления отключены.",
        "Название: Telegram\nКод входа: synthetic-code",
    ],
)
def test_presentation_keeps_short_statuses_plain(status) -> None:
    assert onboarding_presentation(status) == ([], None)


@pytest.mark.parametrize("length", [256, 257, 2048])
def test_presentation_omits_only_address_copy_button_over_256_utf16_units(length) -> None:
    prefix = "https://example.test/"
    endpoint = prefix + "🙂" * ((length - len(prefix)) // 2) + "x" * ((length - len(prefix)) % 2)
    code = "🙂" * 128
    text = onboarding_text(endpoint, linked=True, login_code=code)
    entities, keyboard = onboarding_presentation(text)
    assert keyboard is not None
    buttons = copy_buttons(keyboard)
    assert ("📋 Адрес" in buttons) is (length <= 256)
    assert buttons["📋 Код"] == code
    assert any(entity_text(text, item) == endpoint for item in entities)
    assert any(entity_text(text, item) == code for item in entities)
    assert all(1 <= len(value.encode("utf-16-le")) // 2 <= 256 for value in buttons.values())


def test_missing_public_url_has_no_address_copy_or_download_button() -> None:
    _, keyboard = onboarding_presentation(onboarding_text())
    assert keyboard is not None
    assert set(copy_buttons(keyboard)) == {"📋 Название"}
    assert all("url" not in button for row in keyboard["inline_keyboard"] for button in row)


def test_menus_offer_only_commands_for_the_binding_and_do_not_share_mutable_entries() -> None:
    assert [entry["command"] for entry in command_menu()] == ["connect"]
    assert [entry["command"] for entry in command_menu(linked=True)] == ["connect", "unlink"]
    menu = command_menu()
    menu[0]["command"] = "changed"
    assert command_menu()[0]["command"] == "connect"


def test_current_controls_keep_one_connect_path_and_no_pause_or_duplicate_navigation() -> None:
    assert BUTTON_ACTIONS == {
        "🔌 Подключить AI-приложение": "ui:connect",
        "🔗 Отключить всё": "ui:unlink",
    }
    assert CALLBACK_ACTIONS == {
        "ui:connect",
        "ui:chatgpt",
        "ui:claude",
        "ui:other",
        "ui:help_chatgpt",
        "ui:help_claude",
        "ui:help_other",
        "ui:privacy",
        "ui:unlink",
        "ui:unlink_confirm",
        "ui:cancel",
    }
    for linked in (False, True):
        keyboard = menu_keyboard(linked=linked)
        assert [item["text"] for row in keyboard["keyboard"] for item in row] == (
            ["🔌 Подключить AI-приложение", "🔗 Отключить всё"]
            if linked
            else ["🔌 Подключить AI-приложение"]
        )
    _, picker = client_picker()
    assert [item["callback_data"] for row in picker["inline_keyboard"] for item in row] == [
        "ui:chatgpt",
        "ui:claude",
        "ui:other",
    ]
    for client in ("chatgpt", "claude", "other"):
        text = onboarding_text(PUBLIC_URL, client=client, login_code="synthetic-code")
        _, guide = onboarding_presentation(text)
        buttons = [item for row in guide["inline_keyboard"] for item in row]
        assert {item["text"] for item in buttons if "callback_data" in item} == {
            "Инструкция",
            "Приватность",
        }
        assert {item["callback_data"] for item in buttons if "callback_data" in item} == {
            f"ui:help_{client}",
            "ui:privacy",
        }
        assert all("copy_text" in item or "callback_data" in item for item in buttons)
        _, instructions = help_presentation(client, PUBLIC_URL)
        assert instructions is not None
        assert all("url" in item for row in instructions["inline_keyboard"] for item in row)


@pytest.mark.parametrize("client", [None, "", "unknown"])
def test_help_rejects_unknown_clients_instead_of_rendering_generic_navigation(client) -> None:
    with pytest.raises(ValueError):
        help_presentation(client)


def assert_intro(text: str, *, linked: bool) -> None:
    assert "AI Connector соединяет Telegram с вашим AI-чатом." in text
    assert "уведомления и сообщения" in text
    assert "читать ваши ответы и файлы" in text
    assert "Модель" not in text
    assert "кнопк" in text or "Подключить" in text
    assert "/connect" not in text
    assert "/stop" not in text
    assert "/unlink" not in text
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
    assert bot.sends[-1]["text"].splitlines()[0] == "Связь подтверждена."
    assert_intro(bot.sends[-1]["text"], linked=True)
    assert bot.command_menus[-1]["commands"] == command_menu(linked=True)
    assert service.get_user("synthetic")["active"] is True

    await service.handle_update(command(f"/start {token}"))
    assert bot.sends[-1]["text"].startswith("Ссылка недействительна или устарела.")
    assert token not in bot.sends[-1]["text"]
    assert_intro(bot.sends[-1]["text"], linked=True)

    store.set_active_by_telegram(7001, False)
    await service.handle_update(command("/start"))
    assert bot.sends[-1]["text"].startswith("AI Connector")
    assert service.get_user("synthetic")["active"] is True
    assert_intro(bot.sends[-1]["text"], linked=True)
    await service.handle_update(command("/start"))
    assert bot.sends[-1]["text"].startswith("AI Connector")
    assert_intro(bot.sends[-1]["text"], linked=True)
    assert all("parse_mode" not in sent for sent in bot.sends)


@pytest.mark.parametrize("active", [None, False, True])
@pytest.mark.parametrize("text", ["/connect", "/connect@SYNTHETIC_BOT"])
async def test_connect_is_available_and_restores_only_own_existing_binding(setup, active, text):
    service, store, bot = setup
    if active is not None:
        bind(store)
        store.set_active_by_telegram(7001, active)
    before = service.find_users()
    await service.handle_update(command(text))
    assert len(bot.sends) == 1
    response = bot.sends[0]["text"]
    assert response == onboarding_text(PUBLIC_URL, linked=active is not None)
    assert_guide(response, linked=active is not None)
    expected = [{**user, "active": True} for user in before]
    assert service.find_users() == expected
    assert bot.command_menus == [
        {"commands": command_menu(linked=active is not None), "chat_id": 7001}
    ]


async def test_connect_issues_fresh_codes_only_in_replies_and_keeps_binding(tmp_path) -> None:
    database = tmp_path / "login-code.sqlite3"
    store = Store(database)
    bind(store)
    bot = FakeBot()
    issued_for = []

    def issue(user_key):
        issued_for.append(user_key)
        return f"synthetic-code-{len(issued_for)}"

    service = Service(
        store, bot, "synthetic_bot", public_mcp_url=PUBLIC_URL, login_code_issuer=issue
    )
    with sqlite3.connect(database) as connection:
        before = list(connection.iterdump())
    for index in range(1, 3):
        await service.handle_update(command("/connect"))
        response = bot.sends[-1]["text"]
        assert_guide(response, linked=True)
        assert_code_placement(response, f"synthetic-code-{index}")
    assert "synthetic-code-1" not in bot.sends[-1]["text"]
    assert issued_for == ["synthetic", "synthetic"]
    with sqlite3.connect(database) as connection:
        assert list(connection.iterdump()) == before
    assert service.get_user("synthetic")["active"] is True


async def test_connect_restores_inactive_binding_and_issues_code_without_consuming_invite(
    tmp_path,
) -> None:
    store = Store(tmp_path / "inactive-code.sqlite3")
    bind(store)
    store.set_active_by_telegram(7001, False)
    bot = FakeBot()

    issued = []

    def issue(user_key):
        assert store.get_user(user_key)["active"] is True
        issued.append(user_key)
        return "synthetic-restored-code"

    service = Service(store, bot, "synthetic_bot", login_code_issuer=issue)
    invite = service.create_user_link("synthetic", "Synthetic")
    token = parse_qs(urlsplit(invite["link"]).query)["start"][0]
    before = service.find_users()
    await service.handle_update(command("/connect"))
    assert service.find_users() == [{**user, "active": True} for user in before]
    assert issued == ["synthetic"]
    assert "Код входа: synthetic-restored-code" in bot.sends[-1]["text"]
    await service.handle_update(command(f"/start {token}"))
    assert service.get_user("synthetic")["active"] is True


@pytest.mark.parametrize("active", [False, True])
async def test_connect_preserves_existing_identity_and_profile_metadata(tmp_path, active) -> None:
    store = Store(tmp_path / "preserved-identity.sqlite3")
    invite = store.create_link("operator-owned-key", "Operator supplied display name")
    store.consume_link(invite["token"], 7001, 7001, "stored_username", "Stored name")
    store.set_active_by_telegram(7001, active)
    before = store.get_user("operator-owned-key")
    issued = []

    def issue(user_key):
        issued.append(user_key)
        return "synthetic-preserved-code"

    bot = FakeBot()
    service = Service(store, bot, "synthetic_bot", login_code_issuer=issue)
    update = command("/connect")
    update["message"]["from"].update(username="current_username", first_name="Current name")

    await service.handle_update(update)

    assert store.list_users() == [{**before, "active": True}]
    assert issued == ["operator-owned-key"]
    assert "Код входа: synthetic-preserved-code" in bot.sends[-1]["text"]


@pytest.mark.parametrize("client", ["chatgpt", "claude", "other"])
async def test_explicit_client_selection_restores_only_own_inactive_binding(tmp_path, client):
    store = Store(tmp_path / "restore-client.sqlite3")
    bind(store)
    store.set_active_by_telegram(7001, False)
    other = store.create_link("another", "Another")
    store.consume_link(other["token"], 7002, 7002, None, "Another")
    store.set_active_by_telegram(7002, False)
    issued = []

    def issue(key):
        assert store.get_user(key)["active"] is True
        issued.append(key)
        return "synthetic-restored-code"

    bot = FakeBot()
    service = Service(store, bot, "synthetic_bot", login_code_issuer=issue)
    assert await service.handle_update(callback(f"ui:{client}")) is True
    assert issued == ["synthetic"]
    assert store.get_user("synthetic")["active"] is True
    assert store.get_user("another")["active"] is False
    assert "Код входа: synthetic-restored-code" in bot.sends[-1]["text"]


async def test_connect_never_reactivates_or_issues_for_a_mismatched_binding(tmp_path, monkeypatch):
    store = Store(tmp_path / "mismatched-binding.sqlite3")
    bind(store)
    store.set_active_by_telegram(7001, False)
    before = store.list_users()

    def forbidden(key):
        pytest.fail("A mismatched identity must not receive a login code")

    bot = FakeBot()
    service = Service(store, bot, "synthetic_bot", login_code_issuer=forbidden)
    monkeypatch.setattr(
        service,
        "_binding",
        lambda key: {**before[0], "telegram_user_id": 7002, "chat_id": 7002},
    )
    assert await service.handle_update(command("/connect")) is True
    assert store.list_users() == before
    assert "Код входа:" not in bot.sends[-1]["text"]
    assert "Не удалось получить код входа" in bot.sends[-1]["text"]


async def test_connect_enrollment_failure_is_safe_and_can_be_retried(tmp_path, monkeypatch, caplog):
    store = Store(tmp_path / "enrollment-retry.sqlite3")
    bot = FakeBot()
    issued = []

    def issue(user_key):
        issued.append(user_key)
        return "synthetic-retry-code"

    ensure = store.ensure_private_user

    def fail(*args):
        raise RuntimeError("synthetic-private-enrollment-error")

    monkeypatch.setattr(store, "ensure_private_user", fail)
    service = Service(
        store, bot, "synthetic_bot", public_mcp_url=PUBLIC_URL, login_code_issuer=issue
    )

    await service.handle_update(command("/connect"))

    assert store.list_users() == []
    assert issued == []
    assert_guide(bot.sends[-1]["text"])
    assert (
        "Не удалось получить код входа. Нажмите «🔌 Подключить AI-приложение» ещё раз."
        in bot.sends[-1]["text"]
    )
    assert "Код входа:" not in bot.sends[-1]["text"]
    assert "synthetic-private-enrollment-error" not in str(bot.sends)
    assert "synthetic-private-enrollment-error" not in caplog.text
    assert bot.command_menus[-1]["commands"] == command_menu()
    monkeypatch.setattr(store, "ensure_private_user", ensure)

    await service.handle_update(command("/connect"))

    user = store.get_user_by_telegram(7001)
    assert user is not None and user["active"] is True
    assert issued == [user["user_key"]]
    assert "Код входа: synthetic-retry-code" in bot.sends[-1]["text"]
    assert bot.command_menus[-1]["commands"] == command_menu(linked=True)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize(
    "metadata",
    [
        {"username": 123},
        {"username": {}},
        {"username": "x" * 129},
        {"username": "synthetic\nusername"},
        {"username": "\ud800"},
        {"first_name": None},
        {"first_name": False},
        {"first_name": "x" * 129},
        {"first_name": "synthetic\x00name"},
        {"first_name": "\ud800"},
    ],
)
async def test_invalid_connect_author_metadata_cannot_enroll_or_issue_codes(
    tmp_path, bound, metadata
) -> None:
    store = Store(tmp_path / "invalid-author.sqlite3")
    if bound:
        bind(store)
    before = store.list_users()
    bot = FakeBot()

    def issue(user_key):
        pytest.fail("Malformed author metadata must not issue a login code")

    service = Service(store, bot, "synthetic_bot", login_code_issuer=issue)
    update = command("/connect")
    update["message"]["from"].update(metadata)

    await service.handle_update(update)

    assert store.list_users() == before
    assert bot.sends == []
    assert bot.documents == []
    assert bot.command_menus == []


async def test_login_code_issuer_failure_is_safe_and_command_can_be_retried(
    tmp_path, caplog
) -> None:
    store = Store(tmp_path / "code-failure.sqlite3")
    bind(store)
    bot = FakeBot()
    calls = []

    def issue(user_key):
        calls.append(user_key)
        if len(calls) != 2:
            raise RuntimeError("synthetic-private-error")
        return "synthetic-retry-code"

    service = Service(store, bot, "synthetic_bot", login_code_issuer=issue)
    before = service.find_users()
    await service.handle_update(command("/connect"))
    assert (
        "Не удалось получить код входа. Нажмите «🔌 Подключить AI-приложение» ещё раз."
        in bot.sends[-1]["text"]
    )
    assert "synthetic-private-error" not in bot.sends[-1]["text"]
    assert "synthetic-private-error" not in caplog.text
    assert "Код входа:" not in bot.sends[-1]["text"]
    assert service.find_users() == before
    await service.handle_update(command("/connect"))
    assert "Код входа: synthetic-retry-code" in bot.sends[-1]["text"]
    await service.handle_update(command("/connect"))
    assert bot.sends[-1]["text"].endswith(
        "Не удалось получить код входа. Нажмите «🔌 Подключить AI-приложение» ещё раз."
    )
    assert "synthetic-retry-code" not in bot.sends[-1]["text"]
    assert "Код входа:" not in bot.sends[-1]["text"]
    assert "📋 Код" not in copy_buttons(bot.sends[-1]["reply_markup"])
    assert calls == ["synthetic", "synthetic", "synthetic"]


@pytest.mark.parametrize(
    "code",
    [None, "", " ", "x" * 129, "synthetic\nprivate", "synthetic\rprivate", "\x00", "\ud800"],
)
async def test_invalid_issued_code_uses_safe_retry_response(tmp_path, code) -> None:
    store = Store(tmp_path / "invalid-code.sqlite3")
    bind(store)
    bot = FakeBot()
    service = Service(store, bot, "synthetic_bot", login_code_issuer=lambda user_key: code)
    await service.handle_update(command("/connect"))
    assert bot.sends[-1]["text"].endswith(
        "Не удалось получить код входа. Нажмите «🔌 Подключить AI-приложение» ещё раз."
    )
    assert "Код входа:" not in bot.sends[-1]["text"]


@pytest.mark.parametrize("through_bot", [False, True])
async def test_unlink_revokes_access_before_removing_binding(tmp_path, through_bot) -> None:
    store = Store(tmp_path / "unlink-revoke.sqlite3")
    bind(store)
    bot = FakeBot()
    revoked = []

    def revoke(user_key):
        assert store.get_user(user_key) is not None
        revoked.append(user_key)

    service = Service(store, bot, "synthetic_bot", user_unlinked=revoke)
    if through_bot:
        await service.handle_update(command("/unlink"))
        assert bot.sends[-1]["text"].startswith("Все ваши AI-приложения отключены.")
        assert bot.command_menus[-1]["commands"] == command_menu()
    else:
        assert service.unlink_user("synthetic") == {"user_key": "synthetic", "unlinked": True}
    assert revoked == ["synthetic"]
    assert service.get_user("synthetic") is None
    assert service.unlink_user("synthetic") == {"user_key": "synthetic", "unlinked": False}
    assert revoked == ["synthetic"]


@pytest.mark.parametrize("through_bot", [False, True])
async def test_unlink_revocation_failure_retains_binding_and_allows_safe_retry(
    tmp_path, caplog, through_bot
) -> None:
    store = Store(tmp_path / "unlink-revoke-failure.sqlite3")
    bind(store)
    bot = FakeBot()
    calls = []

    def revoke(user_key):
        calls.append(user_key)
        if len(calls) == 1:
            raise RuntimeError("synthetic-private-revocation-error")

    service = Service(store, bot, "synthetic_bot", user_unlinked=revoke)
    before = service.find_users()
    if through_bot:
        await service.handle_update(command("/unlink"))
        assert bot.sends[-1]["text"] == "Не удалось удалить привязку. Попробуйте ещё раз."
        assert bot.command_menus[-1]["commands"] == command_menu(linked=True)
    else:
        with pytest.raises(ServiceError) as caught:
            service.unlink_user("synthetic")
        assert str(caught.value) == "Could not unlink the user. Try again."
    assert service.find_users() == before
    assert "synthetic-private-revocation-error" not in caplog.text
    assert all("synthetic-private-revocation-error" not in sent["text"] for sent in bot.sends)
    if through_bot:
        await service.handle_update(command("/unlink"))
        assert bot.sends[-1]["text"].startswith("Все ваши AI-приложения отключены.")
    else:
        assert service.unlink_user("synthetic")["unlinked"] is True
    assert service.get_user("synthetic") is None
    assert calls == ["synthetic", "synthetic"]


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
    _, store, bot = setup
    bind(store)

    def issue(user_key):
        pytest.fail("Untrusted updates must not issue login codes")

    service = Service(store, bot, "synthetic_bot", login_code_issuer=issue)
    update = command("/connect")
    update["message"].update(copy.deepcopy(mutation))
    await service.handle_update(update)
    await service.handle_update({"edited_message": command("/connect")["message"]})
    assert bot.sends == []
    assert bot.command_menus == []


async def test_unlink_command_remains_concise(setup) -> None:
    service, store, bot = setup
    bind(store)
    await service.handle_update(command("/unlink"))
    assert len(bot.sends) == 1
    assert "AI Connector" not in bot.sends[0]["text"]
    assert len(bot.sends[0]["text"]) < 150
    assert service.get_user("synthetic") is None
    assert bot.command_menus[-1]["commands"] == command_menu()


async def test_unlinked_unlink_only_points_to_connect_and_does_not_mutate(setup):
    service, _, bot = setup
    link = service.create_user_link("synthetic", "Synthetic")
    token = parse_qs(urlsplit(link["link"]).query)["start"][0]
    await service.handle_update(command("/unlink"))
    assert bot.sends == [
        {
            "chat_id": 7001,
            "text": "Нажмите «🔌 Подключить AI-приложение», чтобы начать подключение.",
            "reply_markup": menu_keyboard(),
        }
    ]
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
    assert service.get_user("synthetic")["active"] is True
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


def test_missing_public_url_uses_owner_instructions() -> None:
    assert Config.from_env({}).public_mcp_url == ""
    text = onboarding_text()
    assert "URL-адрес сервера: адрес подключения запросите у владельца." in text
    assert "📎 Иконка PNG — файлом ниже." in text
    assert "telegram-icon.png" not in text
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


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/" + "x" * 2027,
        "https://example.test/" + "🙂" * 1013 + "x",
        "https://" + ".".join(["x" * 63] * 3 + ["x" * 61]) + ":65535/" + "x" * 1780,
        "https://a" + "\u034f" * 2030 + ".test/mcp",
    ],
)
async def test_longest_public_url_and_connect_guide_fit_one_telegram_message(tmp_path, url) -> None:
    assert len(url.encode("utf-16-le")) // 2 == 2048
    assert len(onboarding_text(url).encode("utf-16-le")) // 2 <= 4096
    bot = FakeBot()
    store = Store(tmp_path / "length.sqlite3")
    bind(store)
    service = Service(
        store,
        bot,
        "synthetic_bot",
        public_mcp_url=url,
        login_code_issuer=lambda user_key: "🙂" * 128,
    )
    await service.handle_update(command("/connect"))
    assert url in bot.sends[0]["text"]
    assert "Код входа:" in bot.sends[0]["text"]
    assert len(bot.sends[0]["text"].encode("utf-16-le")) // 2 <= 4096


async def test_buttons_select_client_before_enrollment_and_issue_only_own_fresh_code(tmp_path):
    store = Store(tmp_path / "button-enrollment.sqlite3")
    bot = FakeBot()
    issued = []

    def issue(user_key):
        issued.append(user_key)
        return f"synthetic-code-{len(issued)}"

    service = Service(
        store, bot, "synthetic_bot", public_mcp_url=PUBLIC_URL, login_code_issuer=issue
    )
    assert await service.handle_update(command("/start")) is True
    assert bot.sends[-1]["reply_markup"] == menu_keyboard()
    assert await service.handle_update(command(button("ui:connect"))) is True
    text, markup = client_picker()
    assert bot.sends[-1] == {"chat_id": 7001, "text": text, "reply_markup": markup}
    assert store.list_users() == issued == bot.documents == []

    for index, (client, heading) in enumerate(
        [
            ("claude", "Подключение к Claude"),
            ("chatgpt", "Подключение к ChatGPT"),
            ("other", "Другое AI-приложение"),
        ],
        1,
    ):
        assert await service.handle_update(callback(f"ui:{client}")) is True
        user = store.get_user_by_telegram(7001)
        assert user is not None and user["active"] is True
        assert issued == [user["user_key"]] * index
        guide = bot.sends[-1]
        assert guide["text"].startswith(heading)
        assert f"Код входа: synthetic-code-{index}\n" in guide["text"]
        assert copy_buttons(guide["reply_markup"])["📋 Код"] == f"synthetic-code-{index}"
        assert bot.documents[-1]["reply_markup"] == menu_keyboard(linked=True)
    assert bot.acknowledgements == ["synthetic-query"] * 3
    assert len(store.list_users()) == 1


@pytest.mark.parametrize(
    "action",
    [
        "ui:menu",
        "ui:connect",
        "ui:help",
        "ui:help_chatgpt",
        "ui:help_claude",
        "ui:help_other",
        "ui:privacy",
        "ui:cancel",
    ],
)
@pytest.mark.parametrize("active", [False, True])
async def test_navigation_never_enrolls_issues_codes_or_revokes(tmp_path, action, active):
    store = Store(tmp_path / "navigation.sqlite3")
    bind(store)
    store.set_active_by_telegram(7001, active)
    bot = FakeBot()
    before = store.list_users()

    def forbidden(user_key):
        pytest.fail("Navigation must not issue a code or revoke access")

    service = Service(
        store, bot, "synthetic_bot", login_code_issuer=forbidden, user_unlinked=forbidden
    )
    assert await service.handle_update(callback(action)) is True
    assert store.list_users() == before
    assert len(bot.sends) == 1
    assert bot.documents == []
    assert bot.acknowledgements == ["synthetic-query"]
    if action.startswith("ui:help_"):
        assert bot.sends[-1]["text"] == help_presentation(action.removeprefix("ui:help_"))[0]


@pytest.mark.parametrize("action", ["ui:chatgpt", "ui:stop", "ui:start", "ui:help", "ui:menu"])
@pytest.mark.parametrize(
    "mutation",
    [
        {"id": None},
        {"id": True},
        {"id": ""},
        {"id": "x" * 257},
        {"id": "synthetic query"},
        {"id": "synthetic\nquery"},
        {"id": "🙂"},
        {"data": "ui:chatgpt:7002"},
        {"data": "ui:chatgpt "},
        {"data": {}},
        {"inline_message_id": "synthetic-inline"},
        {"from": {"id": 7002, "is_bot": False}},
        {"from": {"id": 7001, "is_bot": True}},
        {"from": {"id": True, "is_bot": False}},
        {"from": {"id": 7001, "is_bot": False, "first_name": "bad\x00name"}},
        {"from": {"id": 7001, "is_bot": False, "username": {}}},
        {"message": None},
    ],
)
async def test_untrusted_callback_queries_never_acknowledge_or_act(tmp_path, mutation, action):
    store = Store(tmp_path / "unsafe-callback.sqlite3")
    bot = FakeBot()

    def forbidden(user_key):
        pytest.fail("Untrusted callbacks must not issue codes")

    service = Service(store, bot, "synthetic_bot", login_code_issuer=forbidden)
    update = callback(action)
    update["callback_query"].update(copy.deepcopy(mutation))
    assert await service.handle_update(update) is False
    assert store.list_users() == bot.sends == bot.documents == bot.acknowledgements == []


@pytest.mark.parametrize("action", ["ui:chatgpt", "ui:stop", "ui:start", "ui:help", "ui:menu"])
@pytest.mark.parametrize(
    "mutation",
    [
        {"chat": {"type": "group", "id": -100123}},
        {"chat": {"type": "supergroup", "id": -100123}},
        {"chat": {"type": "private", "id": 7002}},
        {"chat": {"type": "private", "id": True}},
        {"from": {"id": 9999, "is_bot": False, "username": "synthetic_bot"}},
        {"from": {"id": 9999, "is_bot": True, "username": "foreign_bot"}},
        {"from": {"id": True, "is_bot": True, "username": "synthetic_bot"}},
        {"from": {"id": 9999, "is_bot": True}},
        {"message_id": True},
        {"message_id": 0},
        {"date": 0},
        {"date": True},
        {"forward_origin": {}},
        {"forward_from": {}},
        {"via_bot": {}},
        {"sender_chat": {}},
        {"edit_date": 1},
        {"business_connection_id": "synthetic"},
    ],
)
async def test_callbacks_require_accessible_own_bot_message_in_senders_private_chat(
    setup, mutation, action
):
    service, store, bot = setup
    update = callback(action)
    update["callback_query"]["message"].update(copy.deepcopy(mutation))
    assert await service.handle_update(update) is False
    assert store.list_users() == bot.sends == bot.documents == bot.acknowledgements == []


@pytest.mark.parametrize("label", [*BUTTON_ACTIONS, "⏸ Пауза", "🔔 Включить", "ℹ️ Помощь"])
@pytest.mark.parametrize(
    "mutation",
    [
        {"forward_origin": {}},
        {"chat": {"type": "group", "id": -100123}},
        {"from": {"id": 7001, "is_bot": False, "username": []}},
    ],
)
async def test_every_reply_keyboard_alias_uses_private_and_safe_author_gates(
    setup, label, mutation
):
    service, store, bot = setup
    update = command(label)
    update["message"].update(copy.deepcopy(mutation))
    assert await service.handle_update(update) is False
    assert store.list_users() == bot.sends == bot.command_menus == []


@pytest.mark.parametrize("failure", [TelegramAPIError(400), TelegramTransportError()])
async def test_callback_acknowledgement_failure_is_sanitized_and_never_retried(
    setup, caplog, failure
):
    service, _, bot = setup

    async def fail(query_id):
        bot.acknowledgements.append(query_id)
        raise failure

    bot.answer_callback_query = fail
    assert await service.handle_update(callback("ui:menu", query_id="!" * 256)) is True
    assert bot.acknowledgements == ["!" * 256]
    assert len(bot.sends) == 1
    assert caplog.records[-1].message == "Telegram callback acknowledgement could not be sent."


@pytest.mark.parametrize("active", [None, False, True])
@pytest.mark.parametrize(
    "control",
    [
        "/stop",
        "/stop@SYNTHETIC_BOT",
        "⏸ Пауза",
        "🔔 Включить",
        "ℹ️ Помощь",
        "ui:stop",
        "ui:start",
        "ui:help",
        "ui:menu",
    ],
)
async def test_retired_controls_only_show_current_welcome_without_mutating_state(
    tmp_path, active, control
):
    store = Store(tmp_path / "button-optin.sqlite3")
    if active is not None:
        bind(store)
        store.set_active_by_telegram(7001, active)
    bot = FakeBot()

    def forbidden(key):
        pytest.fail("Retired controls must not issue codes or revoke access")

    service = Service(
        store,
        bot,
        "synthetic_bot",
        login_code_issuer=forbidden,
        user_unlinked=forbidden,
    )
    before = store.list_users()
    update = callback(control) if control.startswith("ui:") else command(control)
    assert await service.handle_update(update) is True
    assert store.list_users() == before
    assert_intro(bot.sends[-1]["text"], linked=active is not None)
    assert bot.sends[-1]["reply_markup"] == menu_keyboard(linked=active is not None)
    assert "Код входа:" not in bot.sends[-1]["text"]
    assert bot.documents == []
    assert bot.command_menus[-1]["commands"] == command_menu(linked=active is not None)
    assert bot.acknowledgements == (["synthetic-query"] if control.startswith("ui:") else [])


async def test_unlink_confirmation_only_revokes_current_senders_binding_once(setup):
    _, store, bot = setup
    bind(store)
    another = store.create_link("another", "Another")
    store.consume_link(another["token"], 7002, 7002, None, "Another")
    revoked = []
    service = Service(store, bot, "synthetic_bot", user_unlinked=revoked.append)
    assert await service.handle_update(callback("ui:unlink_confirm")) is True
    assert store.get_user("synthetic") is not None
    assert await service.handle_update(command(button("ui:unlink"))) is True
    assert store.get_user("synthetic") is not None and revoked == []
    assert await service.handle_update(callback("ui:unlink_confirm", telegram_id=7002)) is True
    assert store.get_user("another") is not None and revoked == []
    assert await service.handle_update(callback("ui:unlink_confirm")) is True
    assert store.get_user("synthetic") is None
    assert revoked == ["synthetic"]
    assert await service.handle_update(callback("ui:unlink_confirm")) is True
    assert revoked == ["synthetic"]
    assert store.get_user("another") is not None


@pytest.mark.parametrize("invalidator", ["cancel", "expired", "replacement"])
async def test_unlink_confirmation_cancels_expires_and_cannot_revoke_replacement(
    setup, monkeypatch, invalidator
):
    service, store, _ = setup
    bind(store)
    monkeypatch.setattr("telegram_ping_mcp.service.monotonic", lambda: 100.0)
    await service.handle_update(callback("ui:unlink"))
    if invalidator == "cancel":
        await service.handle_update(callback("ui:cancel"))
    elif invalidator == "expired":
        monkeypatch.setattr("telegram_ping_mcp.service.monotonic", lambda: 221.0)
    else:
        store.delete_user("synthetic")
        new = store.create_link("replacement", "Replacement")
        store.consume_link(new["token"], 7001, 7001, None, "Replacement")
    await service.handle_update(callback("ui:unlink_confirm"))
    assert store.get_user_by_telegram(7001) is not None


async def test_unlink_confirmation_memory_is_bounded(setup, monkeypatch):
    service, store, _ = setup
    bind(store)
    monkeypatch.setattr("telegram_ping_mcp.service.monotonic", lambda: 100.0)
    service._pending_unlinks = {key: ("synthetic-other", 200.0) for key in range(1, 1001)}
    await service.handle_update(callback("ui:unlink"))
    assert len(service._pending_unlinks) == 1000
    assert 1 not in service._pending_unlinks
    assert 7001 in service._pending_unlinks


async def test_unrecognized_updates_return_false_for_inbox_routing(setup):
    service, _, bot = setup
    for update in [
        None,
        {},
        command("Synthetic ordinary message"),
        {"edited_message": command("/start")["message"]},
    ]:
        assert await service.handle_update(update) is False
    assert bot.sends == []
