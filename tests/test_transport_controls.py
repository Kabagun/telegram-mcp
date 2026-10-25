import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from telegram_ping_mcp.telegram import TelegramClient, TelegramTransportError

TOKEN = "123:synthetic-token"


async def test_upload_sends_exact_bytes_caption_and_reply_without_parse_mode():
    requests = []

    def api(request):
        requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": 7001}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        result = await TelegramClient(TOKEN, client=http).send_document_bytes(
            7001,
            b"synthetic file",
            file_name="report.txt",
            mime_type="text/plain",
            caption="Plain <caption>",
            reply_to_message_id=3,
            disable_notification=True,
        )
    assert result["message_id"] == 9
    assert len(requests) == 1 and requests[0].url.path.endswith("/sendDocument")
    request = requests[0]
    body = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    parts = {
        part.get_param("name", header="content-disposition"): part for part in body.iter_parts()
    }
    assert parts["document"].get_payload(decode=True) == b"synthetic file"
    assert parts["document"].get_filename() == "report.txt"
    assert parts["caption"].get_content() == "Plain <caption>"
    assert parts["disable_notification"].get_content() == "true"
    assert json.loads(parts["reply_parameters"].get_content()) == {
        "message_id": 3,
        "allow_sending_without_reply": False,
    }
    assert "parse_mode" not in parts


@pytest.mark.parametrize(
    "change",
    [
        {"chat_id": -1},
        {"data": b""},
        {"file_name": "../private.txt"},
        {"file_name": "report\r\n.txt"},
        {"mime_type": "text/plain\r\nX-Test: bad"},
        {"caption": "🙂" * 513},
        {"reply_to_message_id": True},
        {"disable_notification": 1},
    ],
)
async def test_invalid_upload_never_reaches_telegram(change):
    def forbidden(request):
        pytest.fail("Invalid upload must not reach Telegram")

    params = {
        "chat_id": 7001,
        "data": b"synthetic",
        "file_name": "report.txt",
        "mime_type": "text/plain",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(ValueError):
            await TelegramClient(TOKEN, client=http).send_document_bytes(**(params | change))


async def test_form_edit_and_delete_are_single_attempts_and_preserve_exact_target():
    requests = []

    def api(request):
        requests.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        result = (
            True
            if request.url.path.endswith("/deleteMessage")
            else {"message_id": 8, "chat": {"id": 7001}}
        )
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        bot = TelegramClient(TOKEN, client=http)
        await bot.edit_message_text(7001, 8, "Synthetic confirmation")
        await bot.delete_message(7001, 8)
    assert requests == [
        ("editMessageText", {"chat_id": 7001, "message_id": 8, "text": "Synthetic confirmation"}),
        ("deleteMessage", {"chat_id": 7001, "message_id": 8}),
    ]


async def test_notification_silencing_is_opt_in():
    payloads = []

    def api(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": 7001}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        bot = TelegramClient(TOKEN, client=http)
        await bot.send_message(7001, "Ordinary")
        await bot.send_message(7001, "Release", disable_notification=True)
    assert "disable_notification" not in payloads[0]
    assert payloads[1]["disable_notification"] is True


async def test_upload_unknown_response_is_not_retried():
    requests = []

    def api(request):
        requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": True, "chat": {"id": 7001}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        with pytest.raises(TelegramTransportError):
            await TelegramClient(TOKEN, client=http).send_document_bytes(
                7001, b"synthetic", file_name="file.txt", mime_type="text/plain"
            )
    assert len(requests) == 1
