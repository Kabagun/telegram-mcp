"""Successful model sends produce a private, metadata-only history index."""

import copy
import sqlite3
from types import SimpleNamespace

import pytest

from telegram_ping_mcp.inbox import InboundController, InboxStore
from telegram_ping_mcp.outbound import OutboundSender
from telegram_ping_mcp.service import Service, ServiceError
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramTransportError

TEXT = "Synthetic sent text: preserve <b>literal</b> 😀 without storing it"
CAPTION = "Synthetic sent file caption without durable storage"
QUOTED = "Synthetic quoted reply body without durable storage"
UPLOAD_BYTES = b"synthetic upload bytes without durable storage"
FILE_ID = "synthetic-bot-file-id"
FILE = {
    "download_url": "https://files.oaiusercontent.com/synthetic?signature=synthetic-secret",
    "file_id": "synthetic-client-file",
    "file_name": "synthetic.txt",
    "mime_type": "text/plain",
}
DEFAULT = object()


def bind(store, key="one", telegram_id=7001):
    link = store.create_link(key, "Synthetic")
    return store.consume_link(link["token"], telegram_id, telegram_id, None, "Synthetic")


class FakeBot:
    def __init__(self):
        self.calls = []
        self.command_menus = []
        self.error = None
        self.outcome = DEFAULT
        self.hook = None

    async def set_commands(self, commands, *, chat_id=None):
        self.command_menus.append((commands, chat_id))

    async def send_message(self, chat_id, text, **options):
        return self._send("text", chat_id, text, options)

    async def send_document_bytes(self, chat_id, data, **options):
        assert data == UPLOAD_BYTES
        return self._send("upload", chat_id, options.get("caption", ""), options)

    async def send_attachment(self, chat_id, kind, file_id, **options):
        assert kind == "document"
        assert file_id == FILE_ID
        return self._send("attachment", chat_id, options.get("caption", ""), options)

    def _send(self, kind, chat_id, text, options):
        self.calls.append({"kind": kind, "chat_id": chat_id, "text": text, **options})
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        if self.outcome is not DEFAULT:
            return copy.deepcopy(self.outcome)
        message = {
            "message_id": 90 + len(self.calls),
            "date": 1200 + len(self.calls),
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            "reply_to_message": {"text": QUOTED},
        }
        if kind == "text":
            message["text"] = text
        else:
            message["caption"] = text
            message["document"] = {
                "file_id": FILE_ID,
                "file_name": "synthetic.txt",
                "mime_type": "text/plain",
                "file_size": len(UPLOAD_BYTES),
                "file_path": FILE["download_url"],
            }
        return message


class FakeDownloader:
    def __init__(self):
        self.calls = []

    async def download(self, file, *, max_bytes):
        self.calls.append((file, max_bytes))
        return UPLOAD_BYTES


@pytest.fixture
def system(tmp_path):
    store = Store(tmp_path / "sent-capture.sqlite3")
    bind(store)
    bind(store, "two", 7002)
    inbox = InboxStore(store.path, now=lambda: 1300.0)
    bot = FakeBot()

    def record(user, message):
        inbox.record_outgoing(user, message)

    service = Service(
        store,
        bot,
        "synthetic_bot",
        allowed_group_chat_ids=frozenset({-100123}),
        sent_message_recorder=record,
    )
    inbound = InboundController(store, inbox, bot)
    downloader = FakeDownloader()
    sender = OutboundSender(
        store,
        bot,
        inbox,
        inbound,
        max_attachment_bytes=1024,
        file_downloader=downloader,
    )
    return SimpleNamespace(
        store=store,
        inbox=inbox,
        bot=bot,
        service=service,
        inbound=inbound,
        sender=sender,
        downloader=downloader,
    )


async def send_text(system, route, request_id="synthetic-send", text=TEXT):
    if route == "service":
        return await system.service.ping_user("one", text, request_id)
    return await system.sender.send(user_key="one", text=text, request_id=request_id)


def assert_no_outgoing_content_persisted(system):
    with sqlite3.connect(system.store.path) as connection:
        dump = "\n".join(connection.iterdump())
        assert all(
            row == (None, None)
            for row in connection.execute(
                "SELECT text, caption FROM inbound_messages WHERE direction = 'outgoing'"
            )
        )
    for content in (TEXT, CAPTION, QUOTED, FILE["download_url"], UPLOAD_BYTES.decode()):
        assert content not in dump
        for file in system.store.path.parent.glob(system.store.path.name + "*"):
            assert content.encode() not in file.read_bytes()


@pytest.mark.parametrize("route", ["service", "outbound"])
async def test_accepted_private_text_is_indexed_before_delivery_finishes(
    system, route, monkeypatch
):
    finish = system.store.finish_delivery

    def finish_after_index(request_id, status, **kwargs):
        if status == "sent":
            row = system.inbox.list_messages(direction="outgoing", include_direction=True)[0]
            assert row["telegram_message_id"] == kwargs["message_id"]
        finish(request_id, status, **kwargs)

    monkeypatch.setattr(system.store, "finish_delivery", finish_after_index)
    result = await send_text(system, route)
    assert result["status"] == "sent"
    assert result["message_id"] == 91
    row = system.inbox.list_messages(direction="outgoing", include_direction=True)[0]
    assert row["user_key"] == "one"
    assert row["telegram_user_id"] == row["chat_id"] == 7001
    assert row["sent_at"] == 1201
    assert row["content_available"] is False
    assert row["content_source"] == "telegram"
    assert row["attachment"] is None
    assert system.inbox.list_messages() == []
    assert_no_outgoing_content_persisted(system)


@pytest.mark.parametrize("route", ["service", "outbound"])
async def test_request_retry_after_restart_does_not_resend_or_duplicate_index(system, route):
    first = await send_text(system, route)
    original = system.inbox.list_messages(direction="outgoing", include_direction=True)
    store = Store(system.store.path)
    inbox = InboxStore(store.path)
    if route == "service":
        restarted = Service(
            store, system.bot, "synthetic_bot", sent_message_recorder=inbox.record_outgoing
        )
        duplicate = await restarted.ping_user("one", TEXT, "synthetic-send")
    else:
        restarted = OutboundSender(
            store,
            system.bot,
            inbox,
            InboundController(store, inbox, system.bot),
            max_attachment_bytes=1024,
        )
        duplicate = await restarted.send(user_key="one", text=TEXT, request_id="synthetic-send")
    assert duplicate == {**first, "duplicate": True}
    assert len(system.bot.calls) == 1
    assert system.inbox.list_messages(direction="outgoing", include_direction=True) == original
    with pytest.raises(ServiceError, match="different delivery"):
        await send_text(system, route, text=TEXT + " changed")
    assert len(system.bot.calls) == 1


@pytest.mark.parametrize("source", ["upload", "accepted_attachment"])
async def test_private_file_send_indexes_ack_metadata_without_caption_or_bytes(system, source):
    options = {"file": FILE}
    if source == "accepted_attachment":
        await system.inbound.handle_update(
            {
                "message": {
                    "message_id": 40,
                    "from": {"id": 7001, "is_bot": False},
                    "chat": {"id": 7001, "type": "private"},
                    "document": {
                        "file_id": FILE_ID,
                        "file_name": "synthetic.txt",
                        "mime_type": "text/plain",
                        "file_size": len(UPLOAD_BYTES),
                    },
                }
            }
        )
        original = system.inbox.list_messages()[0]
        options = {"attachment_id": original["attachment"]["id"]}
    result = await system.sender.send(
        user_key="one", request_id="synthetic-file", text=CAPTION, **options
    )
    assert result["status"] == "sent"
    row = system.inbox.list_messages(direction="outgoing", include_direction=True)[0]
    assert row["telegram_message_id"] == result["message_id"]
    assert row["attachment"] == {
        "id": row["attachment"]["id"],
        "kind": "document",
        "name": "synthetic.txt",
        "mime_type": "text/plain",
        "size": len(UPLOAD_BYTES),
    }
    if source == "accepted_attachment":
        assert row["attachment"]["id"] != original["attachment"]["id"]
        assert system.downloader.calls == []
    else:
        assert len(system.downloader.calls) == 1
    assert_no_outgoing_content_persisted(system)
    duplicate = await system.sender.send(
        user_key="one", request_id="synthetic-file", text=CAPTION, **options
    )
    assert duplicate == {**result, "duplicate": True}
    assert len(system.bot.calls) == 1
    assert len(system.inbox.list_messages(direction="outgoing")) == 1


@pytest.mark.parametrize("route", ["service", "outbound"])
@pytest.mark.parametrize(
    "error,status", [(TelegramAPIError(400), "failed"), (TelegramTransportError(), "unknown")]
)
async def test_rejected_or_unknown_delivery_never_creates_history(system, route, error, status):
    system.bot.error = error
    first = await send_text(system, route)
    duplicate = await send_text(system, route)
    assert first["status"] == duplicate["status"] == status
    assert duplicate["duplicate"] is True
    assert len(system.bot.calls) == 1
    assert system.inbox.list_messages(direction="all") == []
    assert system.store.list_sent_delivery_ids() == []
    assert_no_outgoing_content_persisted(system)


@pytest.mark.parametrize("route", ["service", "outbound"])
@pytest.mark.parametrize("outcome", [None, {}, {"message_id": True}, {"message_id": 0}])
async def test_unusable_acknowledgement_never_creates_history(system, route, outcome):
    system.bot.outcome = outcome
    assert (await send_text(system, route))["status"] == "unknown"
    assert system.inbox.list_messages(direction="all") == []
    assert system.store.list_sent_delivery_ids() == []


async def test_group_delivery_and_private_bot_controls_skip_history_recorder(system):
    result = await system.service.ping_group(-100123, ["one"], TEXT, "synthetic-group")
    assert result["status"] == "sent"
    assert await system.service.handle_update(
        {
            "message": {
                "chat": {"id": 7001, "type": "private"},
                "from": {"id": 7001, "is_bot": False, "first_name": "Synthetic"},
                "text": "/start",
            }
        }
    )
    assert [call["chat_id"] for call in system.bot.calls] == [-100123, 7001]
    assert system.bot.command_menus
    assert system.inbox.list_messages(direction="all") == []
    with sqlite3.connect(system.store.path) as connection:
        assert connection.execute("SELECT request_id FROM deliveries").fetchall() == [
            ("synthetic-group",)
        ]


@pytest.mark.parametrize("route", ["service", "outbound"])
@pytest.mark.parametrize("error_type", [ServiceError, sqlite3.OperationalError])
async def test_history_index_failure_keeps_accepted_status_and_logs_static_warning(
    system, route, error_type, monkeypatch, caplog
):
    def fail(*args):
        raise error_type(TEXT + FILE["download_url"])

    monkeypatch.setattr(system.inbox, "record_outgoing", fail)
    result = await send_text(system, route)
    duplicate = await send_text(system, route)
    assert result["status"] == duplicate["status"] == "sent"
    assert duplicate["duplicate"] is True
    assert system.store.list_sent_delivery_ids() == [91]
    assert len(system.bot.calls) == 1
    assert system.inbox.list_messages(direction="all") == []
    assert [record.getMessage() for record in caplog.records] == [
        "Accepted Telegram send could not be indexed for history."
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert_no_outgoing_content_persisted(system)


@pytest.mark.parametrize("route", ["service", "outbound"])
async def test_reply_to_own_outgoing_uses_native_id_and_indexes_the_new_reply(system, route):
    first = await system.service.ping_user("one", TEXT, "synthetic-original")
    target = system.inbox.list_messages(direction="outgoing", include_direction=True)[0]
    assert target["id"] != first["message_id"]
    assert target["text"] is None
    if route == "service":
        reply = await system.service.reply_message(target, TEXT, "synthetic-reply")
        duplicate = await system.service.reply_message(target, TEXT, "synthetic-reply")
    else:
        reply = await system.sender.send(
            user_key="one", text=TEXT, request_id="synthetic-reply", message_id=target["id"]
        )
        duplicate = await system.sender.send(
            user_key="one", text=TEXT, request_id="synthetic-reply", message_id=target["id"]
        )
    assert reply["status"] == "sent"
    assert duplicate == {**reply, "duplicate": True}
    assert len(system.bot.calls) == 2
    assert system.bot.calls[-1]["reply_to_message_id"] == first["message_id"] == 91
    assert [
        row["telegram_message_id"] for row in system.inbox.list_messages(direction="outgoing")
    ] == [91, 92]
    assert_no_outgoing_content_persisted(system)


@pytest.mark.parametrize("target_type", ["other_user", "raw_native_id", "rebound_user"])
async def test_outgoing_reply_target_cannot_cross_a_binding_or_accept_raw_native_id(
    system, target_type
):
    await system.service.ping_user("one", TEXT, "synthetic-original")
    target = system.inbox.list_messages(direction="outgoing", include_direction=True)[0]
    user_key = "one"
    message_id = target["id"]
    if target_type == "other_user":
        user_key = "two"
    elif target_type == "raw_native_id":
        message_id = target["telegram_message_id"]
    else:
        system.store.delete_user("one")
        bind(system.store, "one", 7003)
    with pytest.raises(ServiceError, match="Reply target is unavailable"):
        await system.sender.send(
            user_key=user_key,
            text=TEXT,
            request_id="synthetic-foreign-reply",
            message_id=message_id,
        )
    assert len(system.bot.calls) == 1
    assert len(system.inbox.list_messages(direction="outgoing")) == 1


async def test_service_cannot_reply_to_old_outgoing_row_after_identity_changes(system):
    await system.service.ping_user("one", TEXT, "synthetic-original")
    target = system.inbox.list_messages(direction="outgoing", include_direction=True)[0]
    system.store.delete_user("one")
    bind(system.store, "one", 7003)
    with pytest.raises(ServiceError, match="no longer linked"):
        await system.service.reply_message(target, TEXT, "synthetic-old-identity")
    assert len(system.bot.calls) == 1


@pytest.mark.parametrize("route", ["service", "outbound"])
@pytest.mark.parametrize("transition", ["inactive", "unlinked", "rebound"])
async def test_binding_change_while_send_is_accepted_does_not_index_or_reactivate(
    system, route, transition
):
    def change_binding():
        if transition == "inactive":
            system.store.set_active_by_telegram(7001, False)
        else:
            system.store.delete_user("one")
            if transition == "rebound":
                bind(system.store, "one", 7003)

    system.bot.hook = change_binding
    assert (await send_text(system, route))["status"] == "sent"
    assert system.inbox.list_messages(direction="all") == []
    assert system.store.list_sent_delivery_ids() == [91]
    current = system.store.get_user("one")
    if transition == "inactive":
        assert current["active"] is False
    elif transition == "unlinked":
        assert current is None
    else:
        assert current["telegram_user_id"] == 7003
