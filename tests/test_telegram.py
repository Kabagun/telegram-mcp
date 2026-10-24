import json
import traceback

import httpx
import pytest

from telegram_ping_mcp.telegram import (
    TELEGRAM_MAX_FILE_BYTES,
    TelegramAPIError,
    TelegramClient,
    TelegramFileError,
    TelegramTransportError,
)

TOKEN = "123456:synthetic-test-token"
PRIVATE_TEXT = "synthetic private message <b>verbatim</b>"


def make_client(handler):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramClient(TOKEN, client=http_client), http_client


async def test_get_me_uses_post_and_preserves_caller_owned_client():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"id": 1, "is_bot": True}})

    bot, http_client = make_client(handler)
    async with http_client, bot:
        assert await bot.get_me() == {"id": 1, "is_bot": True}
    assert calls[0].method == "POST"
    assert calls[0].url == f"https://api.telegram.org/bot{TOKEN}/getMe"
    assert json.loads(calls[0].content) == {}

    # Closing the bot separately must not close the caller's HTTP client.
    bot, http_client = make_client(handler)
    async with http_client:
        await bot.aclose()
        assert not http_client.is_closed
        await bot.get_me()


async def test_owned_client_is_closed():
    bot = TelegramClient(TOKEN)
    async with bot:
        assert not bot._client.is_closed
    assert bot._client.is_closed
    await bot.aclose()


async def test_send_message_is_plain_text_and_supports_explicit_entities_and_thread():
    payloads = []
    message = {"message_id": 5, "chat": {"id": -100}, "text": PRIVATE_TEXT}

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": message})

    bot, http_client = make_client(handler)
    async with http_client:
        assert await bot.send_message(-100, PRIVATE_TEXT) == message
        entities = [{"type": "text_mention", "offset": 0, "length": 3, "user": {"id": 7}}]
        await bot.send_message(-100, "Ada", entities=entities, message_thread_id=8)
    assert payloads == [
        {"chat_id": -100, "text": PRIVATE_TEXT},
        {"chat_id": -100, "text": "Ada", "entities": entities, "message_thread_id": 8},
    ]
    assert all("parse_mode" not in payload for payload in payloads)


async def test_get_updates_requests_selected_types_and_long_read_timeout():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 20}]})

    bot, http_client = make_client(handler)
    async with http_client:
        assert await bot.get_updates(19) == [{"update_id": 20}]
        await bot.get_updates(21, timeout=10)
    assert json.loads(calls[0].content) == {
        "offset": 19,
        "timeout": 25,
        "allowed_updates": ["message", "my_chat_member"],
    }
    assert calls[0].extensions["timeout"]["read"] == 35
    assert calls[1].extensions["timeout"]["read"] == 20


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
async def test_transport_failure_is_sanitized_and_send_is_not_retried(error_type):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise error_type(f"{request.url} {PRIVATE_TEXT}", request=request)

    bot, http_client = make_client(handler)
    with pytest.raises(TelegramTransportError) as caught:
        async with http_client:
            await bot.send_message(1, PRIVATE_TEXT)
    assert calls == 1
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered
    assert PRIVATE_TEXT not in rendered
    assert "api.telegram.org" not in rendered


@pytest.mark.parametrize("code", [400, 401, 403, 409, 429, 500])
async def test_api_rejection_is_sanitized_and_never_retries_sends(code):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            code,
            json={
                "ok": False,
                "error_code": code,
                "description": f"{TOKEN} {PRIVATE_TEXT} {request.url}",
                "parameters": {"retry_after": 12},
            },
        )

    bot, http_client = make_client(handler)
    with pytest.raises(TelegramAPIError) as caught:
        async with http_client:
            await bot.send_message(1, PRIVATE_TEXT)
    assert calls == 1
    assert caught.value.code == code
    assert caught.value.retry_after == 12
    assert caught.value.description == str(caught.value)
    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered
    assert PRIVATE_TEXT not in rendered
    assert "api.telegram.org" not in rendered


@pytest.mark.parametrize("retry_after", [-1, True, "12", {}, None])
async def test_invalid_retry_after_is_ignored(retry_after):
    bot, http_client = make_client(
        lambda _: httpx.Response(
            429,
            json={"ok": False, "error_code": 429, "parameters": {"retry_after": retry_after}},
        )
    )
    with pytest.raises(TelegramAPIError) as caught:
        async with http_client:
            await bot.get_me()
    assert caught.value.retry_after is None


@pytest.mark.parametrize(
    ("status", "body", "expected_code"),
    [
        (200, {"ok": False, "error_code": 429}, 429),
        (403, {"ok": True, "result": {}}, 403),
        (500, {"ok": False, "error_code": True}, 500),
        (200, {"ok": False, "error_code": "400"}, 502),
        (200, {"ok": 1, "result": {}}, 502),
        (200, {"result": {}}, 502),
        (200, {"ok": True}, 502),
        (200, [], 502),
    ],
)
async def test_bad_envelopes_and_http_status_are_rejected(status, body, expected_code):
    bot, http_client = make_client(lambda _: httpx.Response(status, json=body))
    with pytest.raises(TelegramAPIError) as caught:
        async with http_client:
            await bot.get_me()
    assert caught.value.code == expected_code


async def test_non_json_error_is_sanitized():
    bot, http_client = make_client(lambda _: httpx.Response(502, text=f"{TOKEN} {PRIVATE_TEXT}"))
    with pytest.raises(TelegramAPIError) as caught:
        async with http_client:
            await bot.get_me()
    assert caught.value.code == 502
    assert TOKEN not in str(caught.value)
    assert PRIVATE_TEXT not in str(caught.value)


@pytest.mark.parametrize(
    ("method", "result"),
    [
        ("get_me", []),
        ("get_me", {"id": True, "is_bot": True}),
        ("get_me", {"id": 1, "is_bot": False}),
        ("send_message", []),
        ("send_message", {"message_id": True, "chat": {"id": 1}}),
        ("send_message", {"message_id": 1, "chat": {"id": "1"}}),
        ("get_updates", {}),
        ("get_updates", [{"update_id": True}]),
        ("get_updates", [{"update_id": -1}]),
        ("get_updates", ["unexpected"]),
    ],
)
async def test_method_result_shapes_are_validated(method, result):
    bot, http_client = make_client(
        lambda _: httpx.Response(200, json={"ok": True, "result": result})
    )
    expected_error = TelegramTransportError if method == "send_message" else TelegramAPIError
    with pytest.raises(expected_error) as caught:
        async with http_client:
            match method:
                case "get_me":
                    await bot.get_me()
                case "send_message":
                    await bot.send_message(1, PRIVATE_TEXT)
                case "get_updates":
                    await bot.get_updates(0)
    if method != "send_message":
        assert caught.value.code == 502


@pytest.mark.parametrize("body", [{}, {"ok": True}, {"ok": True, "result": {}}, []])
async def test_unusable_send_acknowledgement_is_unknown_not_a_rejection(body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body)

    bot, client = make_client(handler)
    async with client:
        with pytest.raises(TelegramTransportError):
            await bot.send_message(1, PRIVATE_TEXT)
    assert len(calls) == 1


async def test_reply_requires_the_target_message_and_preserves_topic():
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 5, "chat": {"id": -100}},
            },
        )

    bot, client = make_client(handler)
    async with client:
        await bot.send_message(-100, "Synthetic reply", reply_to_message_id=31, message_thread_id=9)
    assert payloads == [
        {
            "chat_id": -100,
            "text": "Synthetic reply",
            "message_thread_id": 9,
            "reply_parameters": {"message_id": 31, "allow_sending_without_reply": False},
        }
    ]


@pytest.mark.parametrize("reply_to", [True, 0, -1, "1", 2**63])
async def test_invalid_reply_target_never_sends(reply_to):
    calls = []
    bot, client = make_client(lambda request: calls.append(request))
    async with client:
        with pytest.raises(ValueError, match="Reply message identifier"):
            await bot.send_message(1, PRIVATE_TEXT, reply_to_message_id=reply_to)
    assert calls == []


async def test_get_file_resolves_metadata_and_download_uses_telegram_file_endpoint():
    calls = []
    metadata = {"file_id": "synthetic-file-id", "file_path": "documents/file_1.txt", "file_size": 4}

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "result": metadata})
        return httpx.Response(200, content=b"test")

    bot, client = make_client(handler)
    async with client:
        assert await bot.get_file("synthetic-file-id") == metadata
        assert await bot.download_file(metadata["file_path"], max_bytes=4) == b"test"
    assert calls[0].url == f"https://api.telegram.org/bot{TOKEN}/getFile"
    assert json.loads(calls[0].content) == {"file_id": "synthetic-file-id"}
    assert calls[1].method == "GET"
    assert calls[1].url == f"https://api.telegram.org/file/bot{TOKEN}/documents/file_1.txt"


@pytest.mark.parametrize(
    "path",
    [
        "https://example.com/secret",
        "//example.com/secret",
        "/documents/file.txt",
        "documents/../file.txt",
        "documents/./file.txt",
        "documents/%2e%2e/file.txt",
        "documents\\file.txt",
        "documents/file.txt?token=secret",
        "documents/file.txt#secret",
        "documents//file.txt",
        "documents/file.txt\n",
        "file.txt",
        "",
        None,
    ],
)
async def test_unsafe_file_paths_never_create_requests(path):
    calls = []
    bot, client = make_client(lambda request: calls.append(request))
    async with client:
        with pytest.raises(ValueError, match="file path"):
            await bot.download_file(path)
    assert calls == []


@pytest.mark.parametrize(
    "result",
    [
        [],
        {},
        {"file_id": "id"},
        {"file_path": "documents/file.txt"},
        {"file_id": "id", "file_path": "https://example.com/file.txt"},
        {"file_id": "id", "file_path": "documents/../file.txt"},
        {"file_id": "id", "file_path": "documents/file.txt", "file_size": -1},
        {"file_id": "id", "file_path": "documents/file.txt", "file_size": True},
    ],
)
async def test_get_file_rejects_malformed_metadata(result):
    bot, client = make_client(lambda _: httpx.Response(200, json={"ok": True, "result": result}))
    async with client:
        with pytest.raises(TelegramAPIError) as caught:
            await bot.get_file("synthetic-file-id")
    assert caught.value.code == 502


@pytest.mark.parametrize("file_id", [None, "", "bad\nidentifier", "non-ascii-😀", "x" * 4097])
async def test_invalid_telegram_file_identifiers_never_create_requests(file_id):
    calls = []
    bot, client = make_client(lambda request: calls.append(request))
    async with client:
        with pytest.raises(ValueError, match="file identifier"):
            await bot.get_file(file_id)
    assert calls == []


@pytest.mark.parametrize("max_bytes", [0, -1, True, "10", TELEGRAM_MAX_FILE_BYTES + 1])
async def test_download_limit_has_a_hard_telegram_cap(max_bytes):
    calls = []
    bot, client = make_client(lambda request: calls.append(request))
    async with client:
        with pytest.raises(ValueError, match="download limit"):
            await bot.download_file("documents/file.txt", max_bytes=max_bytes)
    assert calls == []


class FileStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def test_download_rejects_declared_oversize_without_reading_body():
    stream = FileStream([b"x" * 20])
    bot, client = make_client(
        lambda _: httpx.Response(
            200,
            headers={"content-length": "20"},
            stream=stream,
        )
    )
    async with client:
        with pytest.raises(TelegramFileError):
            await bot.download_file("documents/file.txt", max_bytes=19)
    assert stream.reads == 0
    assert stream.closed is True


async def test_download_bounds_stream_even_without_or_with_false_content_length():
    for headers in [{}, {"content-length": "1"}]:
        stream = FileStream([b"a" * 65536, b"b" * 65536, b"never-read"])
        bot, client = make_client(
            lambda _, h=headers, s=stream: httpx.Response(
                200,
                headers=h,
                stream=s,
            )
        )
        async with client:
            with pytest.raises(TelegramFileError):
                await bot.download_file("documents/file.txt", max_bytes=65536)
        assert stream.reads == 2
        assert stream.closed is True


async def test_download_does_not_follow_redirects_or_expose_response_body():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            302, headers={"location": "https://example.com/secret"}, text=f"{TOKEN} {PRIVATE_TEXT}"
        )

    bot, client = make_client(handler)
    async with client:
        with pytest.raises(TelegramAPIError) as caught:
            await bot.download_file("documents/file.txt")
    assert len(calls) == 1
    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered
    assert PRIVATE_TEXT not in rendered
    assert "example.com" not in rendered


async def test_download_network_error_hides_token_and_file_url():
    def handler(request):
        raise httpx.ReadTimeout(f"{request.url} {PRIVATE_TEXT}", request=request)

    bot, client = make_client(handler)
    async with client:
        with pytest.raises(TelegramTransportError) as caught:
            await bot.download_file("documents/file.txt")
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered
    assert PRIVATE_TEXT not in rendered
    assert "api.telegram.org" not in rendered


async def test_download_stops_on_an_unreasonably_large_declared_length():
    stream = FileStream([b"never read"])
    bot, client = make_client(
        lambda _: httpx.Response(
            200,
            headers={"content-length": "9" * 5000},
            stream=stream,
        )
    )
    async with client:
        with pytest.raises(TelegramFileError):
            await bot.download_file("documents/file.txt")
    assert stream.reads == 0
    assert stream.closed is True
