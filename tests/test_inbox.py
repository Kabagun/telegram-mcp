from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import traceback
from pathlib import Path

import pytest

from telegram_ping_mcp.inbox import InboundController, InboxStore
from telegram_ping_mcp.service import ServiceError
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramTransportError

FILE_ID = "synthetic-opaque-telegram-file-id"
CONTENT = "Synthetic inbound message: keep <b>literal</b> and 😀."


class FakeBot:
    def __init__(self):
        self.calls = []
        self.content = b"synthetic file body"
        self.metadata = {"file_id": FILE_ID, "file_path": "documents/file_1.txt", "file_size": 19}
        self.error = None
        self.get_file_hook = None
        self.download_hook = None

    async def get_file(self, file_id):
        self.calls.append(("get_file", file_id))
        if self.get_file_hook:
            self.get_file_hook()
        if self.error:
            raise self.error
        return self.metadata

    async def download_file(self, file_path, *, max_bytes):
        self.calls.append(("download_file", file_path, max_bytes))
        if self.download_hook:
            self.download_hook()
        return self.content


def bind(store, key="synthetic", telegram_id=7001):
    link = store.create_link(key, "Synthetic")
    return store.consume_link(link["token"], telegram_id, telegram_id, None, "Example")


@pytest.fixture
def setup(tmp_path):
    database = tmp_path / "inbox.sqlite3"
    store = Store(database)
    bind(store)
    inbox = InboxStore(database, now=lambda: 1234.0)
    bot = FakeBot()
    controller = InboundController(store, inbox, bot, allowed_group_chat_ids=frozenset({-100123}))
    return store, inbox, bot, controller


def update(text=CONTENT, *, message_id=31, chat_id=7001, author_id=7001, chat_type="private"):
    message = {
        "message_id": message_id,
        "date": 1200,
        "chat": {"type": chat_type, "id": chat_id},
        "from": {"id": author_id, "is_bot": False},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": 1, "message": message}


def document(*, size=19, **kwargs):
    value = update(None, **kwargs)
    value["message"]["document"] = {
        "file_id": FILE_ID,
        "file_name": "../synthetic.txt",
        "file_size": size,
        "mime_type": "text/plain",
    }
    value["message"]["caption"] = "Synthetic caption"
    return value


async def test_private_message_persists_exact_body_without_network_or_reply(setup):
    _, inbox, bot, controller = setup
    await controller.handle_update(update())
    messages = inbox.list_messages()
    assert len(messages) == 1
    item = messages[0]
    assert item == {
        "id": 1,
        "user_key": "synthetic",
        "telegram_user_id": 7001,
        "chat_id": 7001,
        "telegram_message_id": 31,
        "topic_id": None,
        "sent_at": 1200,
        "received_at": 1234.0,
        "text": CONTENT,
        "caption": None,
        "read": False,
        "attachment": None,
    }
    assert inbox.get_message(item["id"]) == item
    assert bot.calls == []


async def test_update_redelivery_deduplicates_by_chat_and_telegram_message_id(setup):
    _, inbox, _, controller = setup
    await asyncio.gather(*(controller.handle_update(update()) for _ in range(5)))
    await controller.handle_update(update("Different body", message_id=31))
    assert [item["text"] for item in inbox.list_messages()] == [CONTENT]
    await controller.handle_update(update(message_id=32))
    await controller.handle_update(update(message_id=31, chat_id=-100123, chat_type="supergroup"))
    assert len(inbox.list_messages()) == 3


@pytest.mark.parametrize("text", ["/start", "/stop", "/unlink", "/unknown arg", " /start@otherbot"])
async def test_all_commands_are_ignored(setup, text):
    _, inbox, bot, controller = setup
    await controller.handle_update(update(text))
    value = document()
    value["message"]["caption"] = text
    await controller.handle_update(value)
    assert inbox.list_messages() == []
    assert bot.calls == []


@pytest.mark.parametrize(
    "patch",
    [
        {"from": {"id": 7001, "is_bot": True}},
        {"from": {"id": 7001}},
        {"from": {"id": True, "is_bot": False}},
        {"from": {"id": 7002, "is_bot": False}},
        {"chat": {"type": "private", "id": 7002}},
        {"chat": {"type": "private", "id": True}},
        {"chat": {"type": "channel", "id": -100123}},
        {"chat": {"type": "supergroup", "id": -100124}},
        {"chat": {"type": "group", "id": 7001}},
        {"message_id": True},
        {"message_id": 0},
        {"message_id": 2**63},
        {"date": -1},
        {"message_thread_id": 2},
    ],
)
async def test_invalid_identity_or_message_metadata_is_ignored(setup, patch):
    _, inbox, _, controller = setup
    value = update()
    value["message"].update(patch)
    await controller.handle_update(value)
    assert inbox.list_messages() == []


@pytest.mark.parametrize(
    "field",
    [
        "forward_origin",
        "forward_date",
        "forward_from",
        "forward_from_chat",
        "forward_from_message_id",
        "forward_signature",
        "forward_sender_name",
        "is_automatic_forward",
        "sender_chat",
        "via_bot",
        "edit_date",
        "business_connection_id",
        "sender_business_bot",
    ],
)
async def test_forwarded_edited_bot_and_business_messages_are_ignored(setup, field):
    _, inbox, _, controller = setup
    value = update()
    value["message"][field] = None
    await controller.handle_update(value)
    assert inbox.list_messages() == []


@pytest.mark.parametrize("value", [None, [], {}, {"edited_message": {}}, {"message": []}])
async def test_non_message_updates_are_ignored(setup, value):
    _, inbox, _, controller = setup
    await controller.handle_update(value)
    assert inbox.list_messages() == []


async def test_allowlisted_group_retains_author_and_topic_association(setup):
    _, inbox, _, controller = setup
    value = update(chat_id=-100123, chat_type="supergroup")
    value["message"]["message_thread_id"] = 9
    await controller.handle_update(value)
    item = inbox.list_messages()[0]
    assert (item["user_key"], item["telegram_user_id"], item["chat_id"], item["topic_id"]) == (
        "synthetic",
        7001,
        -100123,
        9,
    )


async def test_inactive_and_unlinked_users_are_not_ingested(setup):
    store, inbox, _, controller = setup
    store.set_active_by_telegram(7001, False)
    await controller.handle_update(update())
    store.delete_user("synthetic")
    await controller.handle_update(update())
    assert inbox.list_messages() == []


async def test_binding_recheck_at_insert_prevents_optout_race(setup, monkeypatch):
    store, inbox, _, controller = setup
    original = store.get_user_by_telegram

    def read_then_opt_out(telegram_id):
        user = original(telegram_id)
        store.set_active_by_telegram(telegram_id, False)
        return user

    monkeypatch.setattr(store, "get_user_by_telegram", read_then_opt_out)
    await controller.handle_update(update())
    assert inbox.list_messages() == []


@pytest.mark.parametrize("body", ["x" * 4097, "😀" * 2049, "bad\x00text", "bad\rtext", "\ud800"])
async def test_oversize_or_unsafe_bodies_are_ignored(setup, body):
    _, inbox, _, controller = setup
    await controller.handle_update(update(body))
    assert inbox.list_messages() == []


async def test_text_and_caption_limits_and_empty_updates(setup):
    _, inbox, _, controller = setup
    await controller.handle_update(update("😀" * 2048))
    value = document(message_id=32)
    value["message"]["caption"] = "x" * 1025
    await controller.handle_update(value)
    await controller.handle_update(update(None, message_id=33))
    await controller.handle_update(update("  \n\t", message_id=34))
    assert len(inbox.list_messages()) == 1


async def test_attachment_metadata_is_public_but_file_id_and_bytes_are_internal(setup):
    _, inbox, bot, controller = setup
    await controller.handle_update(document())
    item = inbox.list_messages()[0]
    attachment = item["attachment"]
    assert set(attachment) == {"id", "kind", "name", "mime_type", "size"}
    assert attachment["id"] != FILE_ID
    assert len(attachment["id"]) == 32
    assert attachment["name"] == "synthetic.txt"
    assert item["caption"] == "Synthetic caption"
    assert FILE_ID not in json.dumps(item)
    assert bot.calls == []
    result = await controller.read_attachment(attachment["id"])
    assert result == {
        "attachment_id": attachment["id"],
        "message_id": item["id"],
        "data": bot.content,
        "mime_type": "text/plain",
        "name": "synthetic.txt",
        "size": len(bot.content),
    }
    assert bot.calls == [
        ("get_file", FILE_ID),
        ("download_file", "documents/file_1.txt", 20 * 1024 * 1024),
    ]
    with sqlite3.connect(inbox.path) as connection:
        stored = connection.execute("SELECT * FROM inbound_messages").fetchone()
    assert bot.content not in stored
    assert "documents/file_1.txt" not in stored


@pytest.mark.parametrize("kind", ["photo", "audio", "video", "voice", "animation"])
async def test_supported_media_and_photo_variant_selection(setup, kind):
    _, inbox, bot, controller = setup
    value = update(None)
    media = {"file_id": FILE_ID}
    value["message"][kind] = [{"file_id": "small"}, media] if kind == "photo" else media
    if kind == "animation":
        value["message"]["document"] = copy.deepcopy(media)
    await controller.handle_update(value)
    item = inbox.list_messages()[0]
    assert item["attachment"]["kind"] == kind
    await controller.read_attachment(item["attachment"]["id"])
    assert bot.calls[0] == ("get_file", FILE_ID)


@pytest.mark.parametrize(
    "media",
    [
        {"sticker": {"file_id": FILE_ID}},
        {"video_note": {"file_id": FILE_ID}},
        {"document": {}},
        {"document": {"file_id": ""}},
        {"document": {"file_id": "bad\nidentifier"}},
        {"document": {"file_id": FILE_ID, "file_size": -1}},
        {"document": {"file_id": FILE_ID, "file_size": True}},
        {"photo": []},
        {"photo": [None]},
        {"document": {"file_id": FILE_ID}, "audio": {"file_id": FILE_ID}},
    ],
)
async def test_unsupported_or_invalid_media_is_ignored(setup, media):
    _, inbox, bot, controller = setup
    value = update(None)
    value["message"].update(media)
    await controller.handle_update(value)
    assert inbox.list_messages() == []
    assert bot.calls == []


@pytest.mark.parametrize("field", ["sticker", "video_note", "location", "poll", "pinned_message"])
async def test_unsupported_content_is_ignored_even_with_text(setup, field):
    _, inbox, _, controller = setup
    value = update()
    value["message"][field] = {"synthetic": True}
    await controller.handle_update(value)
    assert inbox.list_messages() == []


async def test_unsafe_display_metadata_is_replaced_without_touching_disk(setup):
    _, inbox, _, controller = setup
    value = document()
    value["message"]["document"].update(
        {
            "file_name": "C:\\secret\\..\\synthetic\x00\u202efile.txt",
            "mime_type": "text/plain\nunsafe",
        }
    )
    await controller.handle_update(value)
    metadata = inbox.list_messages()[0]["attachment"]
    assert metadata["name"] == "syntheticfile.txt"
    assert metadata["mime_type"] == "application/octet-stream"


async def test_list_filters_pagination_mark_read_and_persistence(setup):
    store, inbox, _, controller = setup
    bind(store, "second", 7002)
    for value in [update(), update(message_id=32), update(chat_id=7002, author_id=7002)]:
        await controller.handle_update(value)
    assert [item["id"] for item in inbox.list_messages(after_id=1, limit=1)] == [2]
    assert len(inbox.list_messages(user_key="synthetic", chat_id=7001)) == 2
    assert inbox.list_messages(user_key="second")[0]["telegram_user_id"] == 7002
    assert inbox.list_messages(chat_id=-100123) == []
    assert inbox.mark_read(1) == {"message_id": 1, "read": True}
    assert inbox.mark_read(999) == {"message_id": 999, "read": False}
    assert InboxStore(inbox.path).get_message(1)["read"] is True
    assert inbox.get_message(999) is None
    store.set_active_by_telegram(7001, False)
    assert len(inbox.list_messages(user_key="synthetic")) == 2


@pytest.mark.parametrize(
    "arguments",
    [
        {"after_id": -1},
        {"after_id": True},
        {"after_id": 2**63},
        {"limit": 0},
        {"limit": 101},
        {"limit": "1"},
        {"chat_id": 0},
        {"chat_id": True},
        {"chat_id": "7001"},
        {"user_key": ""},
        {"user_key": "x" * 129},
        {"user_key": True},
    ],
)
def test_public_filters_have_bounded_validation(setup, arguments):
    with pytest.raises(ServiceError):
        setup[1].list_messages(**arguments)


@pytest.mark.parametrize("message_id", [-1, 0, True, "1", 2**63])
def test_message_ids_are_validated(setup, message_id):
    with pytest.raises(ServiceError):
        setup[1].get_message(message_id)
    with pytest.raises(ServiceError):
        setup[1].mark_read(message_id)


async def test_declared_oversize_file_is_rejected_before_network(setup):
    _, inbox, bot, controller = setup
    await controller.handle_update(document(size=20))
    with pytest.raises(ServiceError, match="exceeds"):
        await controller.read_attachment(inbox.list_messages()[0]["attachment"]["id"], max_bytes=19)
    assert bot.calls == []


async def test_resolved_oversize_file_is_rejected_before_download(setup):
    _, inbox, bot, controller = setup
    await controller.handle_update(document(size=None))
    bot.metadata["file_size"] = 20
    with pytest.raises(ServiceError, match="exceeds"):
        await controller.read_attachment(inbox.list_messages()[0]["attachment"]["id"], max_bytes=19)
    assert bot.calls == [("get_file", FILE_ID)]


@pytest.mark.parametrize("transition", ["stop", "unlink", "rebind", "group_removed"])
async def test_file_access_requires_current_matching_active_binding(setup, transition):
    store, inbox, bot, controller = setup
    await controller.handle_update(document(chat_id=-100123, chat_type="supergroup"))
    attachment_id = inbox.list_messages()[0]["attachment"]["id"]
    if transition == "stop":
        store.set_active_by_telegram(7001, False)
    elif transition == "unlink":
        store.delete_user("synthetic")
    elif transition == "rebind":
        store.delete_user("synthetic")
        bind(store, telegram_id=7002)
    else:
        controller = InboundController(store, inbox, bot)
    with pytest.raises(ServiceError, match="unavailable"):
        await controller.read_attachment(attachment_id)
    assert bot.calls == []


@pytest.mark.parametrize("phase", ["get_file", "download"])
async def test_optout_during_file_read_does_not_return_body(setup, phase):
    store, inbox, bot, controller = setup
    await controller.handle_update(document())
    attachment_id = inbox.list_messages()[0]["attachment"]["id"]

    def hook():
        store.set_active_by_telegram(7001, False)

    if phase == "get_file":
        bot.get_file_hook = hook
    else:
        bot.download_hook = hook
    with pytest.raises(ServiceError):
        await controller.read_attachment(attachment_id)
    assert len(bot.calls) == (1 if phase == "get_file" else 2)


@pytest.mark.parametrize(
    "error",
    [
        TelegramAPIError(403),
        TelegramTransportError(),
        RuntimeError(FILE_ID),
    ],
)
async def test_attachment_errors_are_fixed_and_content_free(setup, error):
    _, inbox, bot, controller = setup
    await controller.handle_update(document())
    bot.error = error
    with pytest.raises(ServiceError) as caught:
        await controller.read_attachment(inbox.list_messages()[0]["attachment"]["id"])
    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "Telegram attachment could not be downloaded."
    assert FILE_ID not in rendered
    assert "documents/file_1.txt" not in rendered


@pytest.mark.parametrize("metadata", [None, [], {}, {"file_size": True}])
async def test_malformed_file_metadata_fails_with_safe_error(setup, metadata):
    _, inbox, bot, controller = setup
    await controller.handle_update(document())
    bot.metadata = metadata
    with pytest.raises(ServiceError):
        await controller.read_attachment(inbox.list_messages()[0]["attachment"]["id"])
    assert len(bot.calls) == 1


@pytest.mark.parametrize("max_bytes", [0, -1, True, "1", 100 * 1024 * 1024 + 1])
async def test_file_limit_is_bounded(setup, max_bytes):
    _, _, bot, controller = setup
    with pytest.raises(ServiceError):
        await controller.read_attachment("a" * 32, max_bytes=max_bytes)
    assert bot.calls == []


@pytest.mark.parametrize("attachment_id", [None, "", FILE_ID, "../secret", "a" * 33])
async def test_attachment_handles_are_validated(setup, attachment_id):
    with pytest.raises(ServiceError):
        await setup[3].read_attachment(attachment_id)


def test_controller_rejects_separate_unverified_database(setup, tmp_path: Path):
    with pytest.raises(ValueError, match="verified users database"):
        InboundController(setup[0], InboxStore(tmp_path / "other.sqlite3"), setup[2])


async def test_cloud_large_file_explains_local_api_requirement_before_network(setup):
    _, inbox, bot, controller = setup
    await controller.handle_update(document(size=21 * 1024 * 1024))
    attachment_id = inbox.list_messages()[0]["attachment"]["id"]
    info = controller.attachment_info(attachment_id)
    assert info["requires_local_bot_api"] is True
    assert info["max_download_bytes"] == 20 * 1024 * 1024
    assert info["download_path"] == f"/attachments/{attachment_id}"
    assert FILE_ID not in json.dumps(info)
    with pytest.raises(ServiceError, match="local Telegram Bot API"):
        await controller.read_attachment(attachment_id)
    assert bot.calls == []


async def test_local_api_uses_configured_100_mib_limit(setup):
    _, inbox, bot, controller = setup
    bot.max_file_bytes = 100 * 1024 * 1024
    await controller.handle_update(document(size=100 * 1024 * 1024))
    attachment_id = inbox.list_messages()[0]["attachment"]["id"]
    info = controller.attachment_info(attachment_id)
    assert info["max_download_bytes"] == 100 * 1024 * 1024
    assert info["requires_local_bot_api"] is False
    assert bot.calls == []
    await controller.read_attachment(attachment_id)
    assert bot.calls[-1] == ("download_file", "documents/file_1.txt", 100 * 1024 * 1024)


async def test_configured_limit_is_enforced_for_local_files(setup):
    store, inbox, bot, _ = setup
    bot.max_file_bytes = 100 * 1024 * 1024
    controller = InboundController(store, inbox, bot, max_attachment_bytes=30)
    await controller.handle_update(document(size=31))
    attachment_id = inbox.list_messages()[0]["attachment"]["id"]
    assert controller.attachment_info(attachment_id)["max_download_bytes"] == 30
    with pytest.raises(ServiceError, match="exceeds"):
        await controller.read_attachment(attachment_id)
    with pytest.raises(ServiceError, match="Numeric identifier"):
        await controller.read_attachment(attachment_id, max_bytes=31)
    assert bot.calls == []


async def test_attachment_metadata_requires_current_binding(setup):
    store, inbox, _, controller = setup
    await controller.handle_update(document())
    attachment_id = inbox.list_messages()[0]["attachment"]["id"]
    store.set_active_by_telegram(7001, False)
    with pytest.raises(ServiceError, match="unavailable"):
        controller.attachment_info(attachment_id)
