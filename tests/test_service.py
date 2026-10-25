from __future__ import annotations

import asyncio
import copy
import sqlite3
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from telegram_ping_mcp.service import Service, ServiceError
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramTransportError


class FakeBot:
    def __init__(self, *outcomes):
        self.outcomes = deque(outcomes)
        self.sends = []
        self.command_menus = []

    async def set_commands(self, commands, *, chat_id=None):
        self.command_menus.append({"commands": commands, "chat_id": chat_id})

    async def send_message(self, chat_id, text, **kwargs):
        self.sends.append({"chat_id": chat_id, "text": text, **kwargs})
        if self.outcomes:
            outcome = self.outcomes.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return {"message_id": len(self.sends), "chat": {"id": chat_id}}


class BlockingBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()

    async def send_message(self, chat_id, text, **kwargs):
        self.sends.append({"chat_id": chat_id, "text": text, **kwargs})
        self.entered.set()
        await asyncio.Event().wait()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "service.sqlite3"


@pytest.fixture
def store(database: Path) -> Store:
    return Store(database)


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def service(store: Store, bot: FakeBot) -> Service:
    return Service(store, bot, "synthetic_bot", allowed_group_chat_ids=frozenset({-100123}))


def bind(store: Store, key="synthetic", telegram_id=7001, name="Synthetic"):
    link = store.create_link(key, name)
    return store.consume_link(link["token"], telegram_id, telegram_id, None, "Example")


def token_from(link: dict) -> str:
    return parse_qs(urlparse(link["link"]).query)["start"][0]


def command(text: str, telegram_id=7001) -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"type": "private", "id": telegram_id},
            "from": {"id": telegram_id, "is_bot": False, "first_name": "Example"},
            "text": text,
        },
    }


def test_public_link_search_and_unlink_contract(service: Service, store: Store) -> None:
    invitation = service.create_user_link("synthetic", "Synthetic")
    assert set(invitation) == {"link", "expires_at", "user_key"}
    assert invitation["link"].startswith("https://t.me/synthetic_bot?start=")
    assert service.get_user("synthetic") is None
    assert service.find_users() == []
    store.consume_link(token_from(invitation), 7001, 7001, None, "Example")
    assert service.find_users("SYNTHETIC", 1) == [service.get_user("synthetic")]
    assert service.unlink_user("synthetic") == {"user_key": "synthetic", "unlinked": True}
    assert service.unlink_user("synthetic") == {"user_key": "synthetic", "unlinked": False}


@pytest.mark.parametrize("value", ["", " ", "x" * 129, "synthetic\x00", "\ud800"])
def test_user_keys_have_safe_bounded_validation(service: Service, value: str) -> None:
    with pytest.raises(ServiceError) as error:
        service.create_user_link(value, "Synthetic")
    assert "synthetic" not in str(error.value)
    with pytest.raises(ServiceError):
        service.get_user(value)


@pytest.mark.parametrize("ttl", [0, 59, 86401, True, 60.5])
def test_link_lifetime_is_bounded(service: Service, ttl) -> None:
    with pytest.raises(ServiceError):
        service.create_user_link("synthetic", "Synthetic", ttl)


def test_search_bounds_and_constructor_validation(service: Service, store: Store, bot: FakeBot):
    assert service.find_users(" ") == []
    for query, limit in [("x" * 257, 50), ("\ud800", 1), ("", 0), ("", 101), ("", True)]:
        with pytest.raises(ServiceError):
            service.find_users(query, limit)
    for username, kwargs in [
        ("bad/name", {}),
        ("synthetic_bot", {"link_ttl_seconds": 59}),
        ("synthetic_bot", {"allowed_group_chat_ids": frozenset({7001})}),
    ]:
        with pytest.raises(ServiceError):
            Service(store, bot, username, **kwargs)


async def test_private_send_is_deduplicated_and_does_not_persist_text(
    service: Service, store: Store, bot: FakeBot, database: Path
) -> None:
    bind(store)
    text = "Synthetic <b>literal</b> notification & example"
    result = await service.ping_user("synthetic", text, "example-request")
    assert result == {
        "status": "sent",
        "message_id": 1,
        "request_id": "example-request",
        "duplicate": False,
    }
    restarted = Service(Store(database), bot, "synthetic_bot")
    assert await restarted.ping_user("synthetic", text, "example-request") == {
        **result,
        "duplicate": True,
    }
    assert len(bot.sends) == 1
    assert bot.sends[0]["text"] == text
    assert "parse_mode" not in bot.sends[0]
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT * FROM deliveries").fetchone()
        assert row[2] == "sent"
        assert all(text != value for value in row)
    assert text.encode() not in await asyncio.to_thread(database.read_bytes)


async def test_payload_or_resolved_identity_change_cannot_reuse_a_request_id(
    service: Service, store: Store, bot: FakeBot
) -> None:
    bind(store)
    await service.ping_user("synthetic", "Original", "same-id")
    with pytest.raises(ServiceError, match="different delivery"):
        await service.ping_user("synthetic", "Changed", "same-id")
    service.unlink_user("synthetic")
    bind(store, telegram_id=7002)
    with pytest.raises(ServiceError, match="different delivery"):
        await service.ping_user("synthetic", "Original", "same-id")
    assert len(bot.sends) == 1


async def test_concurrent_duplicate_and_restart_never_resend_sending(database: Path) -> None:
    store = Store(database)
    bind(store)
    bot = BlockingBot()
    service = Service(store, bot, "synthetic_bot")
    task = asyncio.create_task(service.ping_user("synthetic", "Example", "in-flight"))
    await asyncio.wait_for(bot.entered.wait(), timeout=1)
    restarted_bot = FakeBot()
    restarted = Service(Store(database), restarted_bot, "synthetic_bot")
    try:
        result = await restarted.ping_user("synthetic", "Example", "in-flight")
        assert result == {
            "status": "sending",
            "message_id": None,
            "request_id": "in-flight",
            "duplicate": True,
        }
        assert len(bot.sends) == 1
        assert restarted_bot.sends == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    result = await restarted.ping_user("synthetic", "Example", "in-flight")
    assert result["status"] == "unknown"
    assert result["duplicate"] is True
    assert restarted_bot.sends == []


@pytest.mark.parametrize(
    "failure,status", [(TelegramAPIError(429, 1), "failed"), (TelegramTransportError(), "unknown")]
)
async def test_failed_and_ambiguous_sends_are_never_retried(
    database: Path, failure, status
) -> None:
    store = Store(database)
    bind(store)
    bot = FakeBot(failure)
    service = Service(store, bot, "synthetic_bot")
    result = await service.ping_user("synthetic", "Example", "one-attempt")
    assert result["status"] == status
    assert result["duplicate"] is False
    again = await Service(Store(database), bot, "synthetic_bot").ping_user(
        "synthetic", "Example", "one-attempt"
    )
    assert again == {**result, "duplicate": True}
    assert len(bot.sends) == 1


async def test_dm_403_disables_user_and_keeps_failed_duplicate_accessible(store: Store) -> None:
    bind(store)
    bot = FakeBot(TelegramAPIError(403))
    service = Service(store, bot, "synthetic_bot")
    result = await service.ping_user("synthetic", "Example", "blocked")
    assert result["status"] == "failed"
    assert service.get_user("synthetic")["active"] is False
    assert await service.ping_user("synthetic", "Example", "blocked") == {
        **result,
        "duplicate": True,
    }
    with pytest.raises(ServiceError):
        await service.ping_user("synthetic", "Example", "another")
    assert len(bot.sends) == 1


async def test_group_mentions_use_numeric_ids_and_utf16_offsets(
    service: Service, store: Store, bot: FakeBot
) -> None:
    bind(store, "first", 7001, "🛰️ <Example>")
    bind(store, "second", 7002, "Other & 🙂")
    result = await service.ping_group(-100123, ["first", "second"], "🚀 Example", "group", 9)
    assert result["status"] == "sent"
    payload = bot.sends[0]
    assert payload["chat_id"] == -100123
    assert payload["message_thread_id"] == 9
    assert payload["text"] == "🚀 Example\n\n🛰️ <Example>, Other & 🙂"
    assert "parse_mode" not in payload
    encoded = payload["text"].encode("utf-16-le")
    for entity, expected_id, expected_name in zip(
        payload["entities"], [7001, 7002], ["🛰️ <Example>", "Other & 🙂"], strict=True
    ):
        assert entity["type"] == "text_mention"
        assert entity["user"]["id"] == expected_id
        assert (
            encoded[entity["offset"] * 2 : (entity["offset"] + entity["length"]) * 2].decode(
                "utf-16-le"
            )
            == expected_name
        )
    with pytest.raises(ServiceError, match="different delivery"):
        await service.ping_group(-100123, ["first", "second"], "🚀 Example", "group", 10)
    with pytest.raises(ServiceError, match="different delivery"):
        await service.ping_group(-100123, ["second", "first"], "🚀 Example", "group", 9)
    assert len(bot.sends) == 1


async def test_group_403_does_not_disable_individual_bindings(store: Store) -> None:
    bind(store, "first", 7001)
    bind(store, "second", 7002)
    bot = FakeBot(TelegramAPIError(403))
    service = Service(store, bot, "synthetic_bot", allowed_group_chat_ids=frozenset({-100123}))
    assert (await service.ping_group(-100123, ["first", "second"], "Example", "group"))[
        "status"
    ] == "failed"
    assert all(user["active"] for user in service.find_users())


@pytest.mark.parametrize("text", ["", " ", "x\x00", "x\ud800", "🙂" * 2049, "x" * 4097])
async def test_invalid_message_text_cannot_send(service: Service, store: Store, bot: FakeBot, text):
    bind(store)
    with pytest.raises(ServiceError):
        await service.ping_user("synthetic", text, "invalid")
    assert bot.sends == []


@pytest.mark.parametrize("request_id", ["", " ", "x" * 129, "example\x00", "\ud800"])
async def test_request_ids_have_safe_bounded_validation(
    service: Service, store: Store, bot: FakeBot, request_id
):
    bind(store)
    with pytest.raises(ServiceError):
        await service.ping_user("synthetic", "Example", request_id)
    assert bot.sends == []


async def test_text_limit_is_utf16_and_group_limit_includes_names(
    service: Service, store: Store, bot: FakeBot
) -> None:
    bind(store)
    assert (await service.ping_user("synthetic", "🙂" * 2048, "boundary"))["status"] == "sent"
    with pytest.raises(ServiceError):
        await service.ping_group(-100123, ["synthetic"], "x" * 4090, "too-long")
    assert len(bot.sends) == 1


@pytest.mark.parametrize(
    "chat_id,keys,topic",
    [
        (7001, ["synthetic"], None),
        (-100124, ["synthetic"], None),
        (-100123, [], None),
        (-100123, ["synthetic", "synthetic"], None),
        (-100123, ["synthetic"] * 51, None),
        (-100123, ["synthetic"], 0),
        (-100123, ["synthetic"], True),
    ],
)
async def test_invalid_group_targets_and_users_cannot_send(
    service: Service, store: Store, bot: FakeBot, chat_id, keys, topic
):
    bind(store)
    with pytest.raises(ServiceError):
        await service.ping_group(chat_id, keys, "Example", "invalid", topic)
    assert bot.sends == []


async def test_unverified_or_inactive_users_cannot_receive_any_notification(
    service: Service, store: Store, bot: FakeBot
) -> None:
    service.create_user_link("pending", "Pending")
    bind(store)
    store.set_active_by_telegram(7001, False)
    with pytest.raises(ServiceError):
        await service.ping_user("pending", "Example", "pending")
    with pytest.raises(ServiceError):
        await service.ping_user("synthetic", "Example", "stopped")
    with pytest.raises(ServiceError):
        await service.ping_group(-100123, ["synthetic"], "Example", "group-stopped")
    assert bot.sends == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"chat": {"id": -100123, "type": "group"}},
        {"chat": {"id": 7002, "type": "private"}},
        {"from": {"id": 7001, "is_bot": True}},
        {"from": {"id": True, "is_bot": False}},
        {"forward_origin": {"type": "user"}},
        {"forward_date": 123},
        {"via_bot": {"id": 9999}},
        {"edit_date": 123},
        {"sender_chat": {"id": -100123}},
    ],
)
async def test_unsafe_updates_cannot_consume_invites_or_trigger_replies(
    service: Service, bot: FakeBot, mutation
):
    token = token_from(service.create_user_link("synthetic", "Synthetic"))
    update = command(f"/start {token}")
    update["message"].update(copy.deepcopy(mutation))
    await service.handle_update(update)
    assert service.get_user("synthetic") is None
    assert bot.sends == []
    await service.handle_update(command(f"/start {token}"))
    assert service.get_user("synthetic")["active"]


@pytest.mark.parametrize(
    "text",
    [
        "Ordinary message",
        " /start",
        "/start ",
        "/start\n",
        "/start invalid extra",
        "/start@other_bot",
        "/stop token",
        "/unlink token",
        "/START",
        "/starter",
    ],
)
async def test_only_exact_supported_commands_receive_replies(service: Service, bot: FakeBot, text):
    await service.handle_update(command(text))
    await service.handle_update({"update_id": 2, "edited_message": command("/start")["message"]})
    assert bot.sends == []
    assert service.find_users() == []


async def test_link_commands_optout_resubscribe_and_unlink_are_safe_and_idempotent(
    service: Service, bot: FakeBot
) -> None:
    await service.handle_update(command("/start"))
    assert bot.sends[-1]["text"].startswith("iConnector")
    assert "/connect" in bot.sends[-1]["text"]
    assert "ссылку-приглашение" not in bot.sends[-1]["text"]
    token = token_from(service.create_user_link("synthetic", "Synthetic"))
    await service.handle_update(command(f"/start@SYNTHETIC_BOT {token}"))
    assert service.get_user("synthetic")["telegram_user_id"] == 7001
    await service.handle_update(command(f"/start {token}"))
    assert "недействительна" in bot.sends[-1]["text"]
    assert token not in bot.sends[-1]["text"]
    for _ in range(2):
        await service.handle_update(command("/stop"))
        assert service.get_user("synthetic")["active"] is False
    await service.handle_update(command("/start"))
    assert service.get_user("synthetic")["active"] is True
    unused = token_from(service.create_user_link("synthetic", "Renamed"))
    for _ in range(2):
        await service.handle_update(command("/unlink"))
        assert service.get_user("synthetic") is None
    await service.handle_update(command(f"/start {unused}"))
    assert service.get_user("synthetic") is None
    assert "недействительна" in bot.sends[-1]["text"]


async def test_start_token_conflict_never_reassigns_a_verified_identity(
    service: Service, store: Store, bot: FakeBot
) -> None:
    bind(store)
    token = token_from(service.create_user_link("another", "Another"))
    await service.handle_update(command(f"/start {token}"))
    assert "недействительна" in bot.sends[-1]["text"]
    assert service.get_user("synthetic")["telegram_user_id"] == 7001
    assert service.get_user("another") is None
    await service.handle_update(command(f"/start {token}", telegram_id=7002))
    assert service.get_user("another")["telegram_user_id"] == 7002


@pytest.mark.parametrize("failure", [TelegramAPIError(429), TelegramTransportError()])
async def test_command_reply_failures_do_not_retry_or_undo_state(store: Store, failure):
    bot = FakeBot(failure)
    service = Service(store, bot, "synthetic_bot")
    token = token_from(service.create_user_link("synthetic", "Synthetic"))
    await service.handle_update(command(f"/start {token}"))
    assert service.get_user("synthetic")["active"]
    assert len(bot.sends) == 1


async def test_command_reply_403_disables_binding(store: Store) -> None:
    bind(store)
    bot = FakeBot(TelegramAPIError(403))
    service = Service(store, bot, "synthetic_bot")
    await service.handle_update(command("/start"))
    assert service.get_user("synthetic")["active"] is False
    assert len(bot.sends) == 1
