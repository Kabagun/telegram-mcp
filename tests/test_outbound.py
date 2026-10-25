from __future__ import annotations

import asyncio
import sqlite3

import pytest

from telegram_ping_mcp.inbox import InboundController, InboxStore
from telegram_ping_mcp.outbound import OutboundSender
from telegram_ping_mcp.service import ServiceError
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramTransportError

FILE = {
    "download_url": "https://files.oaiusercontent.com/synthetic-file?signature=synthetic",
    "file_id": "file-synthetic",
    "file_name": "../example.txt",
    "mime_type": "text/plain",
}


class FakeBot:
    def __init__(self):
        self.calls = []
        self.error = None
        self.metadata_hook = None
        self.result = {"message_id": 89}

    async def send_message(self, *args, **kwargs):
        return await self._send("text", args, kwargs)

    async def send_attachment(self, *args, **kwargs):
        return await self._send("attachment", args, kwargs)

    async def send_document_bytes(self, *args, **kwargs):
        return await self._send("upload", args, kwargs)

    async def _send(self, kind, args, kwargs):
        self.calls.append((kind, args, kwargs))
        if self.error:
            raise self.error
        return self.result

    async def get_file(self, file_id):
        self.calls.append(("get_file", file_id))
        if self.metadata_hook:
            self.metadata_hook()
        return {"file_id": file_id, "file_size": 5}


class FakeDownloader:
    def __init__(self):
        self.calls = []
        self.data = b"synthetic bytes"
        self.hook = None
        self.error = None

    async def download(self, file, *, max_bytes):
        self.calls.append((file, max_bytes))
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        return self.data


def bind(store, key, telegram_id):
    link = store.create_link(key, "Synthetic")
    return store.consume_link(link["token"], telegram_id, telegram_id, None, "Example")


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "outbound.sqlite3")
    bind(store, "one", 7001)
    bind(store, "two", 7002)
    inbox = InboxStore(store.path)
    bot = FakeBot()
    inbound = InboundController(store, inbox, bot, allowed_group_chat_ids=frozenset({-100123}))
    downloader = FakeDownloader()
    sender = OutboundSender(
        store, bot, inbox, inbound, max_attachment_bytes=32, file_downloader=downloader
    )
    return store, inbox, bot, inbound, downloader, sender


async def incoming(inbound, *, telegram_id=7001, chat_id=None, attachment=False, size=5):
    chat_id = telegram_id if chat_id is None else chat_id
    message = {
        "message_id": 42,
        "date": 1200,
        "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
        "from": {"id": telegram_id, "is_bot": False},
    }
    if attachment:
        message["document"] = {
            "file_id": "synthetic-telegram-file-id",
            "file_name": "example.txt",
            "mime_type": "text/plain",
        }
        if size is not None:
            message["document"]["file_size"] = size
    else:
        message["text"] = "Synthetic incoming text"
    await inbound.handle_update({"message": message})


async def test_text_send_is_private_idempotent_and_does_not_persist_body(setup):
    store, _, bot, _, _, sender = setup
    first, second = await asyncio.gather(
        sender.send(user_key="one", request_id="request", text="Synthetic outgoing body"),
        sender.send(user_key="one", request_id="request", text="Synthetic outgoing body"),
    )
    assert first == {
        "status": "sent",
        "message_id": 89,
        "request_id": "request",
        "duplicate": False,
    }
    assert second["duplicate"] is True
    assert bot.calls == [("text", (7001, "Synthetic outgoing body"), {})]
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT * FROM deliveries").fetchall()
    assert "Synthetic outgoing body" not in str(rows)
    assert len(rows[0][1]) == 64
    with pytest.raises(ServiceError, match="different delivery"):
        await sender.send(user_key="two", request_id="request", text="Synthetic outgoing body")


async def test_reply_requires_own_private_inbox_id_and_retains_original_telegram_id(setup):
    _, inbox, bot, inbound, _, sender = setup
    await incoming(inbound)
    target = inbox.list_messages()[0]
    await sender.send(user_key="one", request_id="reply", text="Synthetic", message_id=target["id"])
    assert bot.calls == [("text", (7001, "Synthetic"), {"reply_to_message_id": 42})]
    with pytest.raises(ServiceError, match="Reply target"):
        await sender.send(
            user_key="two", request_id="other", text="Synthetic", message_id=target["id"]
        )
    with pytest.raises(ServiceError, match="Reply target"):
        await sender.send(user_key="one", request_id="raw-id", text="Synthetic", message_id=42)
    await incoming(inbound, chat_id=-100123)
    with pytest.raises(ServiceError, match="Reply target"):
        await sender.send(user_key="one", request_id="group", text="Synthetic", message_id=2)


async def test_stored_attachment_resends_without_implicit_reply_and_is_scoped(setup):
    _, inbox, bot, inbound, _, sender = setup
    await incoming(inbound, attachment=True)
    item = inbox.list_messages()[0]
    await sender.send(user_key="one", request_id="file", attachment_id=item["attachment"]["id"])
    assert bot.calls == [
        ("attachment", (7001, "document", "synthetic-telegram-file-id"), {"caption": ""})
    ]
    with pytest.raises(ServiceError, match="Attachment is unavailable"):
        await sender.send(
            user_key="two", request_id="wrong-user", attachment_id=item["attachment"]["id"]
        )
    with pytest.raises(ServiceError, match="Attachment identifier"):
        await sender.send(
            user_key="one", request_id="raw-file-id", attachment_id="telegram-file-id"
        )


async def test_stored_attachment_can_reply_to_separate_own_inbox_message(setup):
    _, inbox, bot, inbound, _, sender = setup
    await incoming(inbound, attachment=True)
    item = inbox.list_messages()[0]
    await sender.send(
        user_key="one",
        request_id="file-reply",
        attachment_id=item["attachment"]["id"],
        message_id=item["id"],
        text="Synthetic caption",
    )
    assert bot.calls[-1][2] == {"caption": "Synthetic caption", "reply_to_message_id": 42}


async def test_upload_uses_adapter_metadata_and_duplicate_does_not_redownload_expired_url(setup):
    store, _, bot, _, downloader, sender = setup
    first = await sender.send(user_key="one", request_id="upload", file=FILE, text="Caption")
    downloader.error = RuntimeError("synthetic signed URL secret")
    duplicate = await sender.send(
        user_key="one",
        request_id="upload",
        file={**FILE, "download_url": FILE["download_url"] + "-expired"},
        text="Caption",
    )
    assert first["status"] == "sent" and duplicate["duplicate"] is True
    assert len(downloader.calls) == 1
    assert bot.calls == [
        (
            "upload",
            (7001, b"synthetic bytes"),
            {"file_name": "example.txt", "mime_type": "text/plain", "caption": "Caption"},
        )
    ]
    with sqlite3.connect(store.path) as connection:
        saved = str(connection.execute("SELECT * FROM deliveries").fetchall())
    assert all(
        value not in saved
        for value in ("Caption", "synthetic bytes", "signature", "file-synthetic")
    )
    with pytest.raises(ServiceError, match="different delivery"):
        await sender.send(
            user_key="one",
            request_id="upload",
            file={**FILE, "file_id": "file-other"},
            text="Caption",
        )


@pytest.mark.parametrize("action", ["inactive", "unlink", "rebind"])
async def test_upload_rechecks_binding_after_download(setup, action):
    store, _, bot, _, downloader, sender = setup

    def revoke():
        if action == "inactive":
            store.set_active_by_telegram(7001, False)
        else:
            store.delete_user("one")
            if action == "rebind":
                bind(store, "one", 7003)

    downloader.hook = revoke
    with pytest.raises(ServiceError, match="unavailable"):
        await sender.send(user_key="one", request_id="race", file=FILE)
    assert bot.calls == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM deliveries").fetchone()[0] == "failed"


async def test_unknown_size_metadata_rechecks_binding_and_duplicate_skips_metadata(setup):
    store, inbox, bot, inbound, _, sender = setup
    await incoming(inbound, attachment=True, size=None)
    attachment_id = inbox.list_messages()[0]["attachment"]["id"]
    await sender.send(user_key="one", request_id="known", attachment_id=attachment_id)
    await sender.send(user_key="one", request_id="known", attachment_id=attachment_id)
    assert len(bot.calls) == 2
    bot.metadata_hook = lambda: store.set_active_by_telegram(7001, False)
    with pytest.raises(ServiceError, match="unavailable"):
        await sender.send(user_key="one", request_id="revoked", attachment_id=attachment_id)
    assert bot.calls[-1][0] == "get_file"


@pytest.mark.parametrize(
    "text,extra",
    [
        ("", {}),
        (" ", {}),
        ("😀" * 2049, {}),
        ("😀" * 513, {"file": FILE}),
        ("a\x00b", {}),
        ("a\ud800", {}),
        ("text", {"attachment_id": "x", "file": FILE}),
        ("text", {"message_id": True}),
        ("text", {"send_at": "later"}),
        ("text", {"delay_seconds": 0}),
    ],
)
async def test_invalid_request_never_sends_or_downloads(setup, text, extra):
    _, _, bot, _, downloader, sender = setup
    with pytest.raises(ServiceError):
        await sender.send(user_key="one", request_id="invalid", text=text, **extra)
    assert bot.calls == downloader.calls == []


@pytest.mark.parametrize(
    "error,status",
    [
        (TelegramAPIError(403), "failed"),
        (TelegramAPIError(429), "failed"),
        (TelegramTransportError(), "unknown"),
    ],
)
async def test_send_failures_are_sanitized_and_never_retried(setup, error, status):
    store, _, bot, _, _, sender = setup
    bot.error = error
    result = await sender.send(user_key="one", request_id="failure", text="Synthetic")
    assert result["status"] == status
    assert "Synthetic" not in str(result)
    if getattr(error, "code", None) == 403:
        assert store.get_user("one")["active"] is False
        store.set_active_by_telegram(7001, True)
    duplicate = await sender.send(user_key="one", request_id="failure", text="Synthetic")
    assert duplicate["duplicate"] is True and duplicate["status"] == status
    assert len(bot.calls) == 1


async def test_oversized_upload_and_download_errors_fail_before_sending(setup):
    _, _, bot, _, downloader, sender = setup
    downloader.data = b"x" * 33
    with pytest.raises(ServiceError, match="exceeds"):
        await sender.send(user_key="one", request_id="too-big", file=FILE)
    downloader.error = RuntimeError("synthetic signed URL secret")
    with pytest.raises(ServiceError) as caught:
        await sender.send(user_key="one", request_id="bad-download", file=FILE)
    assert "secret" not in str(caught.value)
    assert bot.calls == []


async def test_sensitive_service_error_from_downloader_is_not_persisted(setup):
    store, _, bot, _, downloader, sender = setup
    downloader.error = ServiceError("synthetic signed URL secret")
    with pytest.raises(ServiceError) as caught:
        await sender.send(user_key="one", request_id="sensitive-error", file=FILE)
    assert "secret" not in str(caught.value)
    with sqlite3.connect(store.path) as connection:
        saved = str(connection.execute("SELECT * FROM deliveries").fetchall())
    assert "secret" not in saved
    assert bot.calls == []


async def test_concurrent_upload_duplicates_download_and_send_only_once(setup):
    _, _, bot, _, downloader, sender = setup
    results = await asyncio.gather(
        *(sender.send(user_key="one", request_id="parallel-upload", file=FILE) for _ in range(5))
    )
    assert sum(result["duplicate"] is False for result in results) == 1
    assert len(downloader.calls) == len(bot.calls) == 1


async def test_upload_lock_covers_download_and_send_without_blocking_other_content(setup):
    store, inbox, bot, inbound, downloader, _ = setup
    lock = asyncio.Lock()
    sender = OutboundSender(
        store,
        bot,
        inbox,
        inbound,
        max_attachment_bytes=32,
        file_downloader=downloader,
        download_lock=lock,
    )
    download_entered = asyncio.Event()
    allow_download = asyncio.Event()
    send_entered = asyncio.Event()
    allow_send = asyncio.Event()
    original_download = downloader.download
    original_send = bot.send_document_bytes

    async def download(file, *, max_bytes):
        download_entered.set()
        await allow_download.wait()
        return await original_download(file, max_bytes=max_bytes)

    async def send(*args, **kwargs):
        assert lock.locked()
        send_entered.set()
        await allow_send.wait()
        return await original_send(*args, **kwargs)

    downloader.download = download
    bot.send_document_bytes = send
    first = asyncio.create_task(sender.send(user_key="one", request_id="first-upload", file=FILE))
    await download_entered.wait()
    download_entered.clear()
    second = asyncio.create_task(sender.send(user_key="one", request_id="second-upload", file=FILE))
    await asyncio.sleep(0)
    assert not download_entered.is_set() and not second.done()
    assert downloader.calls == []
    duplicate = await sender.send(user_key="one", request_id="first-upload", file=FILE)
    assert duplicate["duplicate"] is True and duplicate["status"] == "sending"
    text = await sender.send(user_key="one", request_id="text-while-upload", text="Synthetic")
    assert text["status"] == "sent"
    allow_download.set()
    await send_entered.wait()
    assert len(downloader.calls) == 1 and not download_entered.is_set()
    await incoming(inbound, attachment=True)
    attachment_id = inbox.list_messages()[0]["attachment"]["id"]
    stored = await sender.send(
        user_key="one", request_id="stored-while-upload", attachment_id=attachment_id
    )
    assert stored["status"] == "sent"
    allow_send.set()
    results = await asyncio.gather(first, second)
    assert all(result["status"] == "sent" for result in results)
    assert len(downloader.calls) == 2 and not lock.locked()


async def test_binding_is_revalidated_before_a_queued_upload_downloads(setup):
    store, inbox, bot, inbound, downloader, _ = setup
    lock = asyncio.Lock()
    sender = OutboundSender(
        store,
        bot,
        inbox,
        inbound,
        max_attachment_bytes=32,
        file_downloader=downloader,
        download_lock=lock,
    )
    await lock.acquire()
    task = asyncio.create_task(sender.send(user_key="one", request_id="queued-revoke", file=FILE))
    await asyncio.sleep(0)
    store.set_active_by_telegram(7001, False)
    lock.release()
    with pytest.raises(ServiceError, match="unavailable"):
        await task
    assert downloader.calls == bot.calls == []
    assert not lock.locked()


async def test_cancelled_upload_lock_waiter_is_failed_and_never_downloads(setup):
    store, inbox, bot, inbound, downloader, _ = setup
    lock = asyncio.Lock()
    sender = OutboundSender(
        store,
        bot,
        inbox,
        inbound,
        max_attachment_bytes=32,
        file_downloader=downloader,
        download_lock=lock,
    )
    await lock.acquire()
    task = asyncio.create_task(sender.send(user_key="one", request_id="queued-cancel", file=FILE))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    duplicate = await sender.send(user_key="one", request_id="queued-cancel", file=FILE)
    assert duplicate["status"] == "failed" and duplicate["duplicate"] is True
    assert downloader.calls == bot.calls == []
    assert lock.locked()
    lock.release()


async def test_upload_transport_receives_normalized_header_metadata(setup):
    _, _, bot, _, downloader, sender = setup
    await sender.send(
        user_key="one",
        request_id="normalized-headers",
        file={**FILE, "file_name": "../" + "😀" * 100, "mime_type": "!invalid/type"},
    )
    parsed = downloader.calls[0][0]
    assert len(parsed.file_name.encode("utf-8")) <= 255
    assert parsed.mime_type == "application/octet-stream"
    assert bot.calls[0][2]["file_name"] == parsed.file_name
    assert bot.calls[0][2]["mime_type"] == parsed.mime_type


async def test_exact_utf16_boundaries_are_accepted(setup):
    _, _, bot, _, _, sender = setup
    await sender.send(user_key="one", request_id="text-boundary", text="😀" * 2048)
    await sender.send(user_key="one", request_id="caption-boundary", text="😀" * 512, file=FILE)
    assert len(bot.calls) == 2


async def test_cancelled_preparation_fails_without_outgoing_attempt_or_retry(setup):
    _, _, bot, _, downloader, sender = setup
    downloader.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await sender.send(user_key="one", request_id="cancelled-preparation", file=FILE)
    duplicate = await sender.send(user_key="one", request_id="cancelled-preparation", file=FILE)
    assert duplicate["status"] == "failed" and duplicate["duplicate"] is True
    assert len(downloader.calls) == 1 and bot.calls == []


async def test_unexpected_send_error_is_content_free_and_not_retried(setup):
    _, _, bot, _, _, sender = setup
    bot.error = RuntimeError("synthetic API response secret")
    with pytest.raises(ServiceError) as caught:
        await sender.send(user_key="one", request_id="unexpected", text="Synthetic")
    assert "secret" not in str(caught.value)
    duplicate = await sender.send(user_key="one", request_id="unexpected", text="Synthetic")
    assert duplicate["status"] == "unknown" and duplicate["duplicate"] is True
    assert len(bot.calls) == 1


async def test_cancelled_send_is_unknown_and_does_not_retry(setup):
    _, _, bot, _, _, sender = setup
    bot.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await sender.send(user_key="one", request_id="cancelled", text="Synthetic")
    result = await sender.send(user_key="one", request_id="cancelled", text="Synthetic")
    assert result["status"] == "unknown" and result["duplicate"] is True
    assert len(bot.calls) == 1


@pytest.mark.parametrize(
    "result", [None, {"message_id": True}, {"message_id": 0}, {"message_id": "89"}]
)
async def test_malformed_send_acknowledgement_is_unknown(setup, result):
    _, _, bot, _, _, sender = setup
    bot.result = result
    answer = await sender.send(user_key="one", request_id="malformed", text="Synthetic")
    assert answer["status"] == "unknown"
