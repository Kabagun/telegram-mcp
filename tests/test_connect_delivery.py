from __future__ import annotations

import copy
from pathlib import Path

import pytest

from telegram_ping_mcp.onboarding import command_menu, menu_keyboard, onboarding_presentation
from telegram_ping_mcp.service import Service
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramTransportError

PUBLIC_URL = "https://connector.example.test/mcp"
ICON_NOTE = "📎 Иконка PNG — файлом ниже."
ICON_FAILURE = (
    "Не удалось прикрепить иконку. Скачайте PNG кнопкой в инструкции или повторите подключение."
)


class FakeBot:
    def __init__(
        self,
        *,
        guide_failure: Exception | None = None,
        icon_failure: Exception | None = None,
        fallback_failure: Exception | None = None,
    ) -> None:
        self.events: list[dict] = []
        self.sends: list[dict] = []
        self.documents: list[dict] = []
        self.guide_failure = guide_failure
        self.icon_failure = icon_failure
        self.fallback_failure = fallback_failure

    async def send_message(self, chat_id: int, text: str, **kwargs) -> dict:
        event = {"kind": "message", "chat_id": chat_id, "text": text, **kwargs}
        self.events.append(event)
        self.sends.append(event)
        failure = self.guide_failure if len(self.sends) == 1 else self.fallback_failure
        if failure is not None:
            raise failure
        return {"message_id": len(self.events), "chat": {"id": chat_id}}

    async def send_connector_icon(self, chat_id: int, **kwargs) -> dict:
        event = {"kind": "document", "chat_id": chat_id, **kwargs}
        self.events.append(event)
        self.documents.append(event)
        if self.icon_failure is not None:
            raise self.icon_failure
        return {"message_id": len(self.events), "chat": {"id": chat_id}}

    async def set_commands(self, commands: list[dict], *, chat_id: int | None = None) -> None:
        self.events.append({"kind": "menu", "chat_id": chat_id, "commands": commands})


def command(text: str = "/connect") -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"type": "private", "id": 7001},
            "from": {"id": 7001, "is_bot": False, "first_name": "Synthetic"},
            "text": text,
        },
    }


def bind(store: Store, active: bool = True) -> None:
    link = store.create_link("synthetic", "Synthetic")
    store.consume_link(link["token"], 7001, 7001, None, "Synthetic")
    store.set_active_by_telegram(7001, active)


def setup_service(
    tmp_path: Path, *, bot: FakeBot | None = None, active: bool | None = True
) -> tuple[Service, Store, FakeBot, list[str]]:
    store = Store(tmp_path / "connect-delivery.sqlite3")
    if active is not None:
        bind(store, active)
    bot = FakeBot() if bot is None else bot
    issued = []

    def issue(user_key: str) -> str:
        issued.append(user_key)
        return f"synthetic-code-{len(issued)}"

    service = Service(
        store,
        bot,
        "synthetic_bot",
        public_mcp_url=PUBLIC_URL,
        login_code_issuer=issue,
    )
    return service, store, bot, issued


@pytest.mark.parametrize("active", [None, False, True])
@pytest.mark.parametrize("text", ["/connect", "/connect@SYNTHETIC_BOT"])
async def test_connect_sends_guide_icon_menu_and_restores_existing_private_binding(
    tmp_path, active, text
) -> None:
    service, store, bot, issued = setup_service(tmp_path, active=active)
    before = store.list_users()

    await service.handle_update(command(text))

    assert [event["kind"] for event in bot.events] == ["message", "document", "menu"]
    assert all(event["chat_id"] == 7001 for event in bot.events)
    guide = bot.sends[0]
    assert guide["text"].count(ICON_NOTE) == 1
    assert "parse_mode" not in guide
    entities, markup = onboarding_presentation(guide["text"])
    assert guide["entities"] == entities
    if markup is None:
        assert "reply_markup" not in guide
    else:
        assert guide["reply_markup"] == markup
    assert bot.events[-1]["commands"] == command_menu(linked=True)
    assert bot.documents[0]["reply_markup"] == menu_keyboard(linked=True)
    if active is None:
        users = store.list_users()
        assert len(users) == 1
        user = users[0]
        assert user["user_key"].startswith("tg_")
        assert user["telegram_user_id"] == user["chat_id"] == 7001
        assert user["first_name"] == "Synthetic"
        assert user["active"] is True
    else:
        assert store.list_users() == [{**before[0], "active": True}]
        user = store.list_users()[0]
    assert issued == [user["user_key"]]
    assert "Код входа: synthetic-code-1\n" in guide["text"]
    sections = (
        "Название: Telegram\n",
        f"URL-адрес сервера: {PUBLIC_URL}\n",
        "Аутентификация: OAuth\n\n🔑 Код подключения\n",
        "Код входа: synthetic-code-1\n",
        "Вставьте код в окне подключения.\n",
        ICON_NOTE,
    )
    offsets = [guide["text"].index(section) for section in sections]
    assert offsets == sorted(offsets)
    assert "Без срока действия · одноразовый." in guide["text"]
    assert "10 минут" not in guide["text"]
    assert "приглашение" not in guide["text"]
    assert any(entity["type"] == "bold" for entity in entities)
    copies = [
        button["copy_text"]["text"]
        for row in markup["inline_keyboard"]
        for button in row
        if "copy_text" in button
    ]
    assert "synthetic-code-1" in copies
    assert PUBLIC_URL in copies
    assert "Telegram" in copies


@pytest.mark.parametrize(
    "failure", [TelegramAPIError(400), TelegramAPIError(403), TelegramTransportError()]
)
async def test_failed_guide_does_not_send_icon_or_change_binding(tmp_path, failure) -> None:
    service, store, bot, issued = setup_service(tmp_path, bot=FakeBot(guide_failure=failure))
    before = store.list_users()

    await service.handle_update(command())

    assert [event["kind"] for event in bot.events] == ["message", "menu"]
    assert bot.documents == []
    assert issued == ["synthetic"]
    assert store.list_users() == before
    assert store.get_user("synthetic")["active"] is True


@pytest.mark.parametrize(
    "failure",
    [
        TelegramAPIError(403),
        TelegramAPIError(429, 30),
        TelegramTransportError(),
        OSError("synthetic-private-file-path-and-token"),
        ValueError("synthetic-private-chat-and-token"),
    ],
)
async def test_failed_icon_sends_one_safe_fallback_and_preserves_binding(
    tmp_path, caplog, failure
) -> None:
    service, store, bot, issued = setup_service(tmp_path, bot=FakeBot(icon_failure=failure))
    before = store.list_users()

    await service.handle_update(command())

    assert [event["kind"] for event in bot.events] == [
        "message",
        "document",
        "message",
        "menu",
    ]
    assert len(bot.documents) == 1
    assert bot.sends[1] == {
        "kind": "message",
        "chat_id": 7001,
        "text": ICON_FAILURE,
        "reply_markup": menu_keyboard(linked=True),
    }
    assert "synthetic-private" not in str(bot.sends)
    assert "synthetic-private" not in caplog.text
    assert issued == ["synthetic"]
    assert store.list_users() == before
    assert store.get_user("synthetic")["active"] is True


@pytest.mark.parametrize("failure", [TelegramAPIError(403), TelegramTransportError()])
async def test_failed_fallback_does_not_retry_or_deactivate_user(tmp_path, failure) -> None:
    service, store, bot, _ = setup_service(
        tmp_path,
        bot=FakeBot(icon_failure=TelegramTransportError(), fallback_failure=failure),
    )
    before = store.list_users()

    await service.handle_update(command())

    assert [event["kind"] for event in bot.events] == [
        "message",
        "document",
        "message",
        "menu",
    ]
    assert store.list_users() == before


@pytest.mark.parametrize(
    "mutation",
    [
        {"chat": {"type": "group", "id": -7001}},
        {"chat": {"type": "private", "id": 7002}},
        {"from": {"id": 7001, "is_bot": True}},
        {"from": {"id": True, "is_bot": False}},
        {"forward_origin": {}},
        {"forward_from": {}},
        {"sender_chat": {}},
        {"via_bot": {}},
        {"edit_date": 1},
        {"business_connection_id": "synthetic-business"},
        {"text": "/connect@other_bot"},
        {"text": "/connect synthetic-argument"},
        {"text": "/connect\n"},
    ],
)
async def test_untrusted_or_noncommand_connect_updates_send_nothing(tmp_path, mutation) -> None:
    service, store, bot, issued = setup_service(tmp_path, active=None)
    before = store.list_users()
    update = command()
    update["message"].update(copy.deepcopy(mutation))

    await service.handle_update(update)
    await service.handle_update({"edited_message": command()["message"]})

    assert bot.events == []
    assert issued == []
    assert store.list_users() == before


@pytest.mark.parametrize("text", ["/start", "/stop", "/unlink"])
async def test_other_commands_keep_plain_replies_and_never_send_icon(tmp_path, text) -> None:
    service, _, bot, issued = setup_service(tmp_path)

    await service.handle_update(command(text))

    assert [event["kind"] for event in bot.events] == ["message", "menu"]
    assert "entities" not in bot.sends[0]
    assert bot.sends[0]["reply_markup"] == menu_keyboard(linked=text != "/unlink")
    assert bot.documents == []
    assert issued == []


async def test_ordinary_ping_does_not_add_copy_buttons_or_icon(tmp_path) -> None:
    service, _, bot, issued = setup_service(tmp_path)

    result = await service.ping_user("synthetic", "Synthetic notification", "synthetic-request")

    assert result["status"] == "sent"
    assert [event["kind"] for event in bot.events] == ["message"]
    assert "reply_markup" not in bot.sends[0]
    assert bot.documents == []
    assert issued == []
