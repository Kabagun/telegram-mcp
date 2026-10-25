import io
import json
import traceback
from email import policy
from email.parser import BytesParser
from importlib import resources

import httpx
import pytest

from telegram_ping_mcp.onboarding import BUTTON_ACTIONS, CALLBACK_ACTIONS
from telegram_ping_mcp.telegram import TelegramAPIError, TelegramClient, TelegramTransportError

TOKEN = "123456:synthetic-test-token"
PRIVATE_TEXT = "synthetic private text"
CAPTION = "Иконка для поля «Значок»: сохраните файл и загрузите его в ChatGPT."
ICON_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHAT_ID = 7001


def acknowledgement(chat_id=CHAT_ID):
    return {"message_id": 5, "chat": {"id": chat_id}}


def multipart_parts(request):
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + request.headers["content-type"].encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + request.content
    )
    assert message.is_multipart()
    return list(message.iter_parts())


@pytest.mark.parametrize(
    "base_url,expected_url",
    [
        ("https://api.telegram.org", f"https://api.telegram.org/bot{TOKEN}/sendDocument"),
        ("http://localhost:8081/", f"http://127.0.0.1:8081/bot{TOKEN}/sendDocument"),
    ],
)
async def test_connector_icon_uploads_exact_packaged_png_with_fixed_fields(base_url, expected_url):
    calls = []

    def api(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": acknowledgement()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        bot = TelegramClient(TOKEN, client=client, api_base_url=base_url)
        assert await bot.send_connector_icon(CHAT_ID) == acknowledgement()
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert str(request.url) == expected_url
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    parts = multipart_parts(request)
    assert [part.get_param("name", header="content-disposition") for part in parts] == [
        "chat_id",
        "caption",
        "document",
    ]
    assert parts[0].get_payload(decode=True) == str(CHAT_ID).encode("ascii")
    assert parts[1].get_payload(decode=True) == CAPTION.encode("utf-8")
    assert parts[2].get_filename() == "telegram-icon.png"
    assert parts[2].get_content_type() == "image/png"
    expected_bytes = (
        resources.files("telegram_ping_mcp").joinpath("static", "telegram-icon.png").read_bytes()
    )
    assert parts[2].get_payload(decode=True) == expected_bytes
    assert expected_bytes.startswith(ICON_SIGNATURE)
    assert len(expected_bytes) <= 10 * 1024
    assert request.extensions["timeout"] == {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }


async def test_connector_icon_serializes_collapsible_keyboard_in_multipart_field():
    calls = []
    markup = {
        "keyboard": [[{"text": label}] for label in list(BUTTON_ACTIONS)[:4]],
        "resize_keyboard": True,
    }

    def api(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": acknowledgement()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        assert (
            await TelegramClient(TOKEN, client=client).send_connector_icon(
                CHAT_ID, reply_markup=markup
            )
            == acknowledgement()
        )
    assert len(calls) == 1
    parts = multipart_parts(calls[0])
    assert [part.get_param("name", header="content-disposition") for part in parts] == [
        "chat_id",
        "caption",
        "reply_markup",
        "document",
    ]
    assert json.loads(parts[2].get_payload(decode=True)) == markup
    assert parts[3].get_filename() == "telegram-icon.png"


async def test_connector_icon_invalid_keyboard_fails_before_resource_access(monkeypatch):
    def forbidden(*_):
        pytest.fail("Invalid keyboards must fail before resource access or HTTP")

    monkeypatch.setattr("telegram_ping_mcp.telegram.resources.files", forbidden)
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="Telegram reply markup is invalid"):
            await TelegramClient(TOKEN, client=client).send_connector_icon(
                CHAT_ID,
                reply_markup={
                    "keyboard": [[{"text": "Synthetic"}]],
                    "resize_keyboard": True,
                },
            )


@pytest.mark.parametrize("chat_id", [None, 0, -1, True, "7001", 7001.0, 2**63])
async def test_connector_icon_invalid_chat_id_never_reads_file_or_sends(chat_id, monkeypatch):
    def forbidden(*_):
        pytest.fail("Invalid chat identifiers must fail before resource access or HTTP")

    monkeypatch.setattr("telegram_ping_mcp.telegram.resources.files", forbidden)
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="connector icon chat identifier is invalid"):
            await TelegramClient(TOKEN, client=client).send_connector_icon(chat_id)


@pytest.mark.parametrize("chat_id", [1, 2**63 - 1])
async def test_connector_icon_accepts_private_chat_id_boundaries(chat_id):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"ok": True, "result": acknowledgement(chat_id)}
            )
        )
    ) as client:
        assert (
            await TelegramClient(TOKEN, client=client).send_connector_icon(chat_id)
        ) == acknowledgement(chat_id)


class IconResource:
    def __init__(self, data, accesses):
        self.data = data
        self.accesses = accesses

    def joinpath(self, *parts):
        self.accesses.append(("path", parts))
        return self

    def open(self, mode):
        self.accesses.append(("mode", mode))
        accesses = self.accesses

        class IconStream(io.BytesIO):
            def read(self, size=-1):
                accesses.append(("read", size))
                return super().read(size)

        return IconStream(self.data)


@pytest.mark.parametrize("data", [b"", b"not-a-png", ICON_SIGNATURE + b"x" * (10 * 1024)])
async def test_connector_icon_reads_only_bounded_fixed_resource_and_rejects_invalid(
    data, monkeypatch
):
    accesses = []

    def resource(package):
        accesses.append(("package", package))
        return IconResource(data, accesses)

    def forbidden(request):
        pytest.fail("Invalid packaged images must not reach Telegram")

    monkeypatch.setattr("telegram_ping_mcp.telegram.resources.files", resource)
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="Packaged Telegram connector icon"):
            await TelegramClient(TOKEN, client=client).send_connector_icon(CHAT_ID)
    assert accesses == [
        ("package", "telegram_ping_mcp"),
        ("path", ("static", "telegram-icon.png")),
        ("mode", "rb"),
        ("read", 10 * 1024 + 1),
    ]


async def test_missing_connector_icon_does_not_expose_resource_path(monkeypatch):
    private_path = "synthetic-private-directory/telegram-icon.png"

    def resource(_):
        raise OSError(private_path)

    def forbidden(request):
        pytest.fail("Unavailable packaged images must not reach Telegram")

    monkeypatch.setattr("telegram_ping_mcp.telegram.resources.files", resource)
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError) as caught:
            await TelegramClient(TOKEN, client=client).send_connector_icon(CHAT_ID)
    assert caught.value.__context__ is None
    assert private_path not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectError])
async def test_connector_icon_transport_failure_is_sanitized_without_retry(error_type):
    calls = []

    def api(request):
        calls.append(request)
        raise error_type(f"{request.url} {PRIVATE_TEXT}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        with pytest.raises(TelegramTransportError) as caught:
            await TelegramClient(TOKEN, client=client).send_connector_icon(CHAT_ID)
    assert len(calls) == 1
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered
    assert PRIVATE_TEXT not in rendered
    assert "api.telegram.org" not in rendered


@pytest.mark.parametrize("status", [400, 429, 500, 302])
async def test_connector_icon_api_rejection_is_sanitized_without_retry_or_redirect(status):
    calls = []

    def api(request):
        calls.append(request)
        if status == 302:
            return httpx.Response(
                status, headers={"location": "https://attacker.invalid/"}, text=PRIVATE_TEXT
            )
        return httpx.Response(
            status,
            json={
                "ok": False,
                "error_code": status,
                "description": f"{TOKEN} {PRIVATE_TEXT}",
                "parameters": {"retry_after": 3},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        with pytest.raises(TelegramAPIError) as caught:
            await TelegramClient(TOKEN, client=client).send_connector_icon(CHAT_ID)
    assert len(calls) == 1
    assert caught.value.code == status
    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered
    assert PRIVATE_TEXT not in rendered
    assert "attacker.invalid" not in rendered
    if status != 302:
        assert caught.value.retry_after == 3


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"ok": 1, "result": acknowledgement()},
        {"ok": True},
        {"ok": True, "result": None},
        {"ok": True, "result": []},
        {"ok": True, "result": {}},
        {"ok": True, "result": {"message_id": 0, "chat": {"id": CHAT_ID}}},
        {"ok": True, "result": {"message_id": -1, "chat": {"id": CHAT_ID}}},
        {"ok": True, "result": {"message_id": True, "chat": {"id": CHAT_ID}}},
        {"ok": True, "result": {"message_id": 1, "chat": {"id": str(CHAT_ID)}}},
        {"ok": True, "result": {"message_id": 1, "chat": {"id": True}}},
        {"ok": True, "result": acknowledgement(CHAT_ID + 1)},
    ],
)
async def test_connector_icon_unusable_2xx_acknowledgement_is_ambiguous_without_retry(body):
    calls = []

    def api(request):
        calls.append(request)
        return httpx.Response(200, content=b"null" if body is None else json.dumps(body).encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        with pytest.raises(TelegramTransportError):
            await TelegramClient(TOKEN, client=client).send_connector_icon(CHAT_ID)
    assert len(calls) == 1


async def test_message_preserves_copy_and_https_buttons_without_parse_mode_or_rewriting():
    calls = []
    markup = {
        "inline_keyboard": [
            [
                {"text": "Скопировать название", "copy_text": {"text": "Telegram Ping"}},
                {"text": "Скопировать URL", "copy_text": {"text": "https://synthetic.invalid/mcp"}},
            ],
            [{"text": "Открыть ChatGPT", "url": "https://chatgpt.com/"}],
        ]
    }

    def api(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": acknowledgement()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        bot = TelegramClient(TOKEN, client=client)
        await bot.send_message(CHAT_ID, "Synthetic <b>plain</b> guide", reply_markup=markup)
        await bot.send_message(CHAT_ID, "Synthetic plain message", reply_markup=None)
    assert json.loads(calls[0].content) == {
        "chat_id": CHAT_ID,
        "text": "Synthetic <b>plain</b> guide",
        "reply_markup": markup,
    }
    assert json.loads(calls[1].content) == {
        "chat_id": CHAT_ID,
        "text": "Synthetic plain message",
    }


@pytest.mark.parametrize("action", sorted(CALLBACK_ACTIONS))
async def test_message_preserves_each_static_callback_action(action):
    calls = []
    markup = {"inline_keyboard": [[{"text": "Synthetic navigation", "callback_data": action}]]}

    def api(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": acknowledgement()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        await TelegramClient(TOKEN, client=client).send_message(
            CHAT_ID, "Guide", reply_markup=markup
        )
    assert json.loads(calls[0].content)["reply_markup"] == markup


@pytest.mark.parametrize("label", sorted(BUTTON_ACTIONS))
async def test_message_preserves_each_static_reply_keyboard_label(label):
    calls = []
    markup = {
        "keyboard": [[{"text": label}]],
        "resize_keyboard": True,
    }

    def api(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": acknowledgement()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        await TelegramClient(TOKEN, client=client).send_message(
            CHAT_ID, "Guide", reply_markup=markup
        )
    assert json.loads(calls[0].content)["reply_markup"] == markup


@pytest.mark.parametrize(
    "markup",
    [
        {"keyboard": [[{"text": label}]], "resize_keyboard": True, "is_persistent": True}
        for label in list(BUTTON_ACTIONS)[:1]
    ]
    + [
        {
            "keyboard": keyboard,
            "resize_keyboard": resize,
            **({"is_persistent": persistent} if persistent is not True else {}),
            **extra,
        }
        for keyboard, resize, persistent, extra in [
            ([], True, True, {}),
            ([[]], True, True, {}),
            (([{"text": next(iter(BUTTON_ACTIONS))}],), True, True, {}),
            ([[next(iter(BUTTON_ACTIONS))]], True, True, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS))}]] * 5, True, True, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS))}] * 4], True, True, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS))}]], False, True, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS))}]], True, False, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS))}]], 1, True, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS))}]], True, 1, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS))}]], True, True, {"one_time_keyboard": True}),
            ([[{"text": next(iter(BUTTON_ACTIONS))}]], True, True, {"inline_keyboard": []}),
            ([[{"text": "Synthetic"}]], True, True, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS)), "request_contact": True}]], True, True, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS)), "request_location": True}]], True, True, {}),
            ([[{"text": next(iter(BUTTON_ACTIONS)), "callback_data": "ui:menu"}]], True, True, {}),
            (
                [
                    [
                        {
                            "text": next(iter(BUTTON_ACTIONS)),
                            "web_app": {"url": "https://example.invalid/"},
                        }
                    ]
                ],
                True,
                True,
                {},
            ),
        ]
    ],
)
async def test_non_static_or_malformed_reply_keyboard_fails_before_network(markup):
    def forbidden(request):
        pytest.fail("Invalid keyboards must not reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="Telegram reply markup is invalid"):
            await TelegramClient(TOKEN, client=client).send_message(
                CHAT_ID, "Guide", reply_markup=markup
            )


@pytest.mark.parametrize(
    "markup",
    [
        {
            "keyboard": [[{"text": label}]],
            "resize_keyboard": True,
        }
        for label in (
            "🔌 Подключить",
            "⏸ Пауза",
            "🔔 Включить",
            "🔗 Отключить",
            "ℹ️ Помощь",
        )
    ]
    + [
        {"inline_keyboard": [[{"text": "Synthetic navigation", "callback_data": action}]]}
        for action in ("ui:menu", "ui:start", "ui:stop", "ui:help")
    ],
)
async def test_retired_outgoing_controls_fail_before_network(markup):
    def forbidden(request):
        pytest.fail("Retired controls must not reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="Telegram reply markup is invalid"):
            await TelegramClient(TOKEN, client=client).send_message(
                CHAT_ID, "Guide", reply_markup=markup
            )


async def test_keyboard_accepts_row_button_and_utf16_length_boundaries():
    markup = {
        "inline_keyboard": [
            [{"text": "🙂" * 32, "copy_text": {"text": "🙂" * 128}} for _ in range(3)]
            for _ in range(4)
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": acknowledgement()})
        )
    ) as client:
        await TelegramClient(TOKEN, client=client).send_message(
            CHAT_ID, "Guide", reply_markup=markup
        )


@pytest.mark.parametrize("port", [1, 443, 8080, 8443, 65535])
async def test_https_url_buttons_preserve_valid_custom_ports(port):
    calls = []
    markup = {
        "inline_keyboard": [
            [{"text": "Synthetic", "url": f"https://example.invalid:{port}/assets/icon.png"}]
        ]
    }

    def api(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": acknowledgement()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        await TelegramClient(TOKEN, client=client).send_message(
            CHAT_ID, "Guide", reply_markup=markup
        )
    assert json.loads(calls[0].content)["reply_markup"] == markup


@pytest.mark.parametrize(
    "markup",
    [
        {},
        [],
        {"keyboard": [[{"text": "Synthetic"}]]},
        {"inline_keyboard": [], "remove_keyboard": True},
        {"inline_keyboard": []},
        {"inline_keyboard": ()},
        {"inline_keyboard": [[]]},
        {"inline_keyboard": [()]},
        {"inline_keyboard": ["Synthetic"]},
        {"inline_keyboard": [[None]]},
        {"inline_keyboard": [[{"text": "Synthetic", "copy_text": {"text": "Copy"}}]] * 5},
        {"inline_keyboard": [[{"text": "Synthetic", "copy_text": {"text": "Copy"}}] * 4]},
    ],
)
async def test_invalid_keyboard_shapes_fail_before_network(markup):
    def forbidden(request):
        pytest.fail("Invalid keyboards must not reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="Telegram reply markup is invalid"):
            await TelegramClient(TOKEN, client=client).send_message(
                CHAT_ID, "Guide", reply_markup=markup
            )


@pytest.mark.parametrize(
    "button",
    [
        {},
        {"text": "Synthetic"},
        {"text": None, "copy_text": {"text": "Copy"}},
        {"text": "", "copy_text": {"text": "Copy"}},
        {"text": "  ", "copy_text": {"text": "Copy"}},
        {"text": "x" * 65, "copy_text": {"text": "Copy"}},
        {"text": "🙂" * 33, "copy_text": {"text": "Copy"}},
        {"text": "Synthetic\ud800", "copy_text": {"text": "Copy"}},
        {"text": "Synthetic", "copy_text": "Copy"},
        {"text": "Synthetic", "copy_text": {}},
        {"text": "Synthetic", "copy_text": {"text": None}},
        {"text": "Synthetic", "copy_text": {"text": ""}},
        {"text": "Synthetic", "copy_text": {"text": "x" * 257}},
        {"text": "Synthetic", "copy_text": {"text": "🙂" * 129}},
        {"text": "Synthetic", "copy_text": {"text": "Synthetic\ud800"}},
        {"text": "Synthetic", "copy_text": {"text": "Copy", "url": "https://example.invalid/"}},
        {"text": "Synthetic", "copy_text": {"text": "Copy"}, "url": "https://example.invalid/"},
        {"text": "Synthetic", "callback_data": "synthetic"},
        {"text": "Synthetic", "callback_data": None},
        {"text": "Synthetic", "callback_data": []},
        {"text": "Synthetic", "callback_data": "ui:menu:synthetic"},
        {"text": "Synthetic", "callback_data": "ui:menu", "url": "https://example.invalid/"},
        {"text": "Synthetic", "callback_data": "ui:menu", "copy_text": {"text": "Copy"}},
        {"text": "Synthetic", "web_app": {"url": "https://example.invalid/"}},
        {"text": "Synthetic", "login_url": {"url": "https://example.invalid/"}},
        {"text": "Synthetic", "pay": True},
        {"text": "Synthetic", "switch_inline_query": ""},
    ],
)
async def test_invalid_button_capabilities_and_text_fail_before_network(button):
    def forbidden(request):
        pytest.fail("Invalid buttons must not reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="Telegram reply markup is invalid"):
            await TelegramClient(TOKEN, client=client).send_message(
                CHAT_ID, "Guide", reply_markup={"inline_keyboard": [[button]]}
            )


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://example.invalid/",
        "tg://user?id=7001",
        "javascript:synthetic()",
        "//example.invalid/",
        "https:///missing-host",
        "https://user:synthetic@example.invalid/",
        "https://user@example.invalid/",
        "https://@example.invalid/",
        "https://example.invalid/?token=synthetic",
        "https://example.invalid/#synthetic",
        "https://example.invalid:0/",
        "https://example.invalid:-1/",
        "https://example.invalid:invalid/",
        "https://example.invalid:99999/",
        "https://[invalid/",
        "https://example.invalid/\nsynthetic",
        " https://example.invalid/",
        "https://example.invalid/synthetic path",
        "https://example.invalid/\u202esynthetic",
        "https://example.invalid/\ud800",
        "https://example.invalid\\synthetic",
    ],
)
async def test_invalid_url_buttons_fail_before_network(url):
    def forbidden(request):
        pytest.fail("Invalid URL buttons must not reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="Telegram reply markup is invalid"):
            await TelegramClient(TOKEN, client=client).send_message(
                CHAT_ID,
                "Guide",
                reply_markup={"inline_keyboard": [[{"text": "Synthetic", "url": url}]]},
            )
