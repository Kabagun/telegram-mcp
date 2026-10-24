from __future__ import annotations

import asyncio
import json
import sqlite3
import traceback

import httpx
import pytest

from telegram_ping_mcp.config import MAX_ATTACHMENT_BYTES
from telegram_ping_mcp.inbox import InboundController, InboxStore
from telegram_ping_mcp.service import ServiceError
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import (
    TELEGRAM_MAX_FILE_BYTES,
    TelegramAPIError,
    TelegramClient,
    TelegramTransportError,
)

FILE_ID = "synthetic-accepted-file-id"
CAPTION = "Synthetic plain <b>caption</b> 😀"
DEFAULT = object()


class FakeBot:
    def __init__(self):
        self.max_file_bytes = MAX_ATTACHMENT_BYTES
        self.sends = []
        self.lookups = []
        self.metadata = {"file_id": FILE_ID, "file_size": 19}
        self.metadata_hook = None
        self.outcome = DEFAULT
        self.entered = asyncio.Event()
        self.gate = None

    async def get_file(self, file_id):
        self.lookups.append(file_id)
        if self.metadata_hook:
            self.metadata_hook()
        if isinstance(self.metadata, BaseException):
            raise self.metadata
        return self.metadata

    async def send_attachment(self, chat_id, kind, file_id, **options):
        self.sends.append({"chat_id": chat_id, "kind": kind, "file_id": file_id, **options})
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome is not DEFAULT:
            return self.outcome
        return {"message_id": 61, "chat": {"id": chat_id}}


def bind(store, telegram_id=7001):
    invitation = store.create_link("synthetic", "Synthetic")
    store.consume_link(invitation["token"], telegram_id, telegram_id, None, "Synthetic")


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "inbox.sqlite3")
    bind(store)
    inbox = InboxStore(store.path)
    bot = FakeBot()
    controller = InboundController(store, inbox, bot, allowed_group_chat_ids=frozenset({-100123}))
    return store, inbox, bot, controller


async def incoming(
    setup, *, kind="document", size=19, file_id=FILE_ID, chat_id=7001, topic_id=None, message_id=31
):
    message = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
        "from": {"id": 7001, "is_bot": False},
    }
    if kind is not None:
        media = {"file_id": file_id, "file_size": size}
        message[kind] = (
            [{"file_id": "smaller-photo", "file_size": 2}, media] if (kind == "photo") else media
        )
    else:
        message["text"] = "Synthetic message without an attachment"
    if topic_id is not None:
        message["message_thread_id"] = topic_id
    await setup[3].handle_update({"message": message})
    return setup[1].list_messages()[-1]["id"]


async def test_reply_uses_only_the_exact_accepted_file_and_original_group_topic(setup):
    _, _, bot, controller = setup
    message_id = await incoming(setup, chat_id=-100123, topic_id=17)
    result = await controller.reply_attachment(message_id, CAPTION, "file-1")
    assert result == {
        "status": "sent",
        "message_id": 61,
        "request_id": "file-1",
        "duplicate": False,
    }
    assert bot.sends == [
        {
            "chat_id": -100123,
            "kind": "document",
            "file_id": FILE_ID,
            "caption": CAPTION,
            "message_thread_id": 17,
            "reply_to_message_id": 31,
        }
    ]
    assert bot.lookups == []


@pytest.mark.parametrize("transition", ["stop", "unlink", "rebind", "group_removed"])
async def test_reply_requires_current_matching_active_binding_and_allowed_chat(setup, transition):
    store, inbox, bot, controller = setup
    message_id = await incoming(setup, chat_id=-100123, topic_id=17)
    if transition == "stop":
        store.set_active_by_telegram(7001, False)
    elif transition == "unlink":
        store.delete_user("synthetic")
    elif transition == "rebind":
        store.delete_user("synthetic")
        bind(store, 7002)
    else:
        controller = InboundController(store, inbox, bot)
    with pytest.raises(ServiceError, match="unavailable"):
        await controller.reply_attachment(message_id, "", "file-disabled")
    assert bot.sends == bot.lookups == []


@pytest.mark.parametrize("transition", ["stop", "unlink", "rebind"])
async def test_reply_revalidates_binding_after_metadata_lookup(setup, transition):
    store, _, bot, controller = setup
    message_id = await incoming(setup, size=None)

    def change_binding():
        if transition == "stop":
            store.set_active_by_telegram(7001, False)
        else:
            store.delete_user("synthetic")
            if transition == "rebind":
                bind(store, 7002)

    bot.metadata_hook = change_binding
    with pytest.raises(ServiceError, match="unavailable"):
        await controller.reply_attachment(message_id, "", "file-disabled")
    assert bot.lookups == [FILE_ID]
    assert bot.sends == []


async def test_unknown_size_is_resolved_without_downloading_or_changing_file_id(setup):
    _, _, bot, controller = setup
    message_id = await incoming(setup, size=None)
    assert (await controller.reply_attachment(message_id, "", "file-size"))["status"] == "sent"
    assert bot.lookups == [FILE_ID]
    assert bot.sends[0]["file_id"] == FILE_ID


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"file_id": FILE_ID, "file_size": True},
        {"file_id": "different-file", "file_size": 1},
        RuntimeError(FILE_ID),
    ],
)
async def test_unverifiable_size_never_sends_and_hides_metadata(setup, metadata):
    _, _, bot, controller = setup
    message_id = await incoming(setup, size=None)
    bot.metadata = metadata
    with pytest.raises(ServiceError) as caught:
        await controller.reply_attachment(message_id, "", "file-size")
    assert str(caught.value) == "Telegram attachment metadata is unavailable."
    assert FILE_ID not in "".join(traceback.format_exception(caught.value))
    assert bot.sends == []


async def test_exactly_100_mib_can_be_resent_without_loading_bytes(setup):
    message_id = await incoming(setup, size=MAX_ATTACHMENT_BYTES)
    assert (await setup[3].reply_attachment(message_id, "", "file-100"))["status"] == "sent"
    assert setup[2].lookups == []


@pytest.mark.parametrize("source", ["stored", "resolved"])
async def test_files_above_100_mib_are_rejected_before_sending(setup, source):
    _, _, bot, controller = setup
    message_id = await incoming(
        setup, size=MAX_ATTACHMENT_BYTES + 1 if source == "stored" else None
    )
    bot.metadata["file_size"] = MAX_ATTACHMENT_BYTES + 1
    with pytest.raises(ServiceError, match="exceeds the allowed send size"):
        await controller.reply_attachment(message_id, "", "oversized")
    assert bot.sends == []


async def test_send_obeys_configured_limit_and_cloud_api_cap(setup):
    store, inbox, bot, _ = setup
    message_id = await incoming(setup, size=21)
    controller = InboundController(store, inbox, bot, max_attachment_bytes=20)
    with pytest.raises(ServiceError, match="exceeds the allowed send size"):
        await controller.reply_attachment(message_id, "", "configured-limit")
    message_id = await incoming(setup, size=TELEGRAM_MAX_FILE_BYTES + 1, message_id=32)
    bot.max_file_bytes = TELEGRAM_MAX_FILE_BYTES
    controller = InboundController(store, inbox, bot)
    with pytest.raises(ServiceError, match="local Telegram Bot API"):
        await controller.reply_attachment(message_id, "", "cloud-limit")
    assert bot.sends == []


@pytest.mark.parametrize("message_id", [True, 0, -1, "1", 2**63, 100])
async def test_invalid_or_missing_inbound_message_cannot_send(setup, message_id):
    with pytest.raises(ServiceError):
        await setup[3].reply_attachment(message_id, "", "missing-file")
    assert setup[2].sends == []


async def test_text_only_message_cannot_be_used_as_a_file_reply(setup):
    message_id = await incoming(setup, kind=None)
    with pytest.raises(ServiceError, match="unavailable"):
        await setup[3].reply_attachment(message_id, "", "no-file")
    assert setup[2].sends == []


@pytest.mark.parametrize("caption", [None, "x" * 1025, "😀" * 513, "bad\rcontrol", "\ud800"])
async def test_invalid_captions_never_send(setup, caption):
    message_id = await incoming(setup)
    with pytest.raises(ServiceError):
        await setup[3].reply_attachment(message_id, caption, "bad-caption")
    assert setup[2].sends == []


async def test_caption_uses_utf16_limit_and_can_be_blank(setup):
    message_id = await incoming(setup)
    for index, caption in enumerate(["", "😀" * 512, "line\nwith\ttab"]):
        result = await setup[3].reply_attachment(message_id, caption, f"caption-{index}")
        assert result["status"] == "sent"
        assert setup[2].sends[-1]["caption"] == caption


async def test_request_id_deduplicates_concurrent_and_completed_sends(setup):
    _, _, bot, controller = setup
    message_id = await incoming(setup)
    bot.gate = asyncio.Event()
    first = asyncio.create_task(controller.reply_attachment(message_id, CAPTION, "once"))
    await bot.entered.wait()
    pending = await controller.reply_attachment(message_id, CAPTION, "once")
    assert pending == {
        "status": "sending",
        "message_id": None,
        "request_id": "once",
        "duplicate": True,
    }
    bot.gate.set()
    assert (await first)["status"] == "sent"
    assert (await controller.reply_attachment(message_id, CAPTION, "once"))["duplicate"] is True
    assert len(bot.sends) == 1
    with pytest.raises(ServiceError, match="different delivery"):
        await controller.reply_attachment(message_id, "Changed caption", "once")
    second_id = await incoming(setup, message_id=32)
    with pytest.raises(ServiceError, match="different delivery"):
        await controller.reply_attachment(second_id, CAPTION, "once")


async def test_only_hash_and_safe_delivery_status_are_persisted(setup):
    store, _, _, controller = setup
    message_id = await incoming(setup)
    await controller.reply_attachment(message_id, CAPTION, "hash-only")
    with sqlite3.connect(store.path) as connection:
        delivery = connection.execute("SELECT * FROM deliveries").fetchone()
    assert len(delivery[1]) == 64
    assert FILE_ID not in repr(delivery)
    assert CAPTION not in repr(delivery)


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (TelegramAPIError(400), "failed"),
        (TelegramTransportError(), "unknown"),
        ({}, "unknown"),
        ({"message_id": True}, "unknown"),
        ({"message_id": 2**63}, "unknown"),
    ],
)
async def test_rejected_and_unknown_sends_are_never_retried(setup, outcome, status):
    _, _, bot, controller = setup
    message_id = await incoming(setup)
    bot.outcome = outcome
    first = await controller.reply_attachment(message_id, CAPTION, "failed-once")
    assert first["status"] == status
    second = await controller.reply_attachment(message_id, CAPTION, "failed-once")
    assert second["status"] == status
    assert second["duplicate"] is True
    assert len(bot.sends) == 1


async def test_private_chat_block_disables_binding(setup):
    store, _, bot, controller = setup
    message_id = await incoming(setup)
    bot.outcome = TelegramAPIError(403)
    assert (await controller.reply_attachment(message_id, "", "blocked"))["status"] == "failed"
    assert store.get_user("synthetic")["active"] is False


async def test_cancelled_send_remains_unknown_and_does_not_retry(setup):
    _, _, bot, controller = setup
    message_id = await incoming(setup)
    bot.outcome = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await controller.reply_attachment(message_id, "", "cancelled")
    result = await controller.reply_attachment(message_id, "", "cancelled")
    assert result["status"] == "unknown" and result["duplicate"] is True
    assert len(bot.sends) == 1


async def test_unexpected_send_error_is_sanitized_and_cannot_retry(setup):
    _, _, bot, controller = setup
    message_id = await incoming(setup)
    bot.outcome = RuntimeError(f"{FILE_ID} {CAPTION}")
    with pytest.raises(ServiceError) as caught:
        await controller.reply_attachment(message_id, CAPTION, "unexpected")
    rendered = "".join(traceback.format_exception(caught.value))
    assert FILE_ID not in rendered and CAPTION not in rendered
    assert (await controller.reply_attachment(message_id, CAPTION, "unexpected"))["duplicate"]
    assert len(bot.sends) == 1


@pytest.mark.parametrize(
    ("kind", "method"),
    [
        ("document", "sendDocument"),
        ("photo", "sendPhoto"),
        ("audio", "sendAudio"),
        ("video", "sendVideo"),
        ("voice", "sendVoice"),
        ("animation", "sendAnimation"),
    ],
)
async def test_client_uses_original_media_method_and_plain_caption(kind, method):
    calls = []

    def api(request):
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 61, "chat": {"id": -100123}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        bot = TelegramClient("123:synthetic", client=client)
        await bot.send_attachment(
            -100123, kind, FILE_ID, caption=CAPTION, message_thread_id=17, reply_to_message_id=31
        )
    assert len(calls) == 1
    assert calls[0].url.path.endswith("/" + method)
    assert json.loads(calls[0].content) == {
        "chat_id": -100123,
        kind: FILE_ID,
        "caption": CAPTION,
        "message_thread_id": 17,
        "reply_parameters": {"message_id": 31, "allow_sending_without_reply": False},
    }


@pytest.mark.parametrize(
    "file_id",
    [
        "https://example.invalid/file",
        "file:///secret",
        "C:\\secret.bin",
        "/tmp/secret",
        "attach://upload",
        "data:application/octet-stream;base64,eA==",
        "bad\nidentifier",
    ],
)
async def test_client_and_controller_never_accept_external_file_sources(setup, file_id):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        bot = TelegramClient("123:synthetic", client=client)
        with pytest.raises(ValueError, match="file identifier"):
            await bot.send_attachment(7001, "document", file_id)
    assert calls == []
    if "\n" not in file_id:
        message_id = await incoming(setup, file_id=file_id)
        with pytest.raises(ServiceError, match="metadata is invalid"):
            await setup[3].reply_attachment(message_id, "", "external-file")
        assert setup[2].sends == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ok": True},
        {"ok": True, "result": {}},
        {"ok": True, "result": {"message_id": 61, "chat": {"id": 999}}},
    ],
)
async def test_unusable_file_send_acknowledgement_has_unknown_delivery(body):
    calls = []

    def api(request):
        calls.append(request)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        bot = TelegramClient("123:synthetic", client=client)
        with pytest.raises(TelegramTransportError):
            await bot.send_attachment(7001, "document", FILE_ID)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["sticker", "video_note", "sendDocument", "", None])
async def test_client_rejects_unsupported_media_types_without_network(kind):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        bot = TelegramClient("123:synthetic", client=client)
        with pytest.raises(ValueError, match="attachment type"):
            await bot.send_attachment(7001, kind, FILE_ID)
    assert calls == []
