from __future__ import annotations

import json

import httpx
import pytest

from telegram_ping_mcp.telegram import TelegramAPIError, TelegramClient, TelegramTransportError

COMMANDS = [{"command": "connect", "description": "Как подключить AI-чат"}]


@pytest.mark.parametrize(
    "chat_id,scope",
    [(None, {"type": "default"}), (7001, {"type": "chat", "chat_id": 7001})],
)
async def test_command_menu_uses_default_or_private_chat_scope(chat_id, scope) -> None:
    calls = []

    def api(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        bot = TelegramClient("123:synthetic", client=client)
        assert await bot.set_commands(COMMANDS, chat_id=chat_id) is None
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path.endswith("/setMyCommands")
    assert json.loads(calls[0].content) == {"commands": COMMANDS, "scope": scope}


@pytest.mark.parametrize(
    "commands",
    [[], [{"command": f"cmd{index}", "description": "Synthetic"} for index in range(100)]],
)
async def test_command_menu_supports_empty_and_maximum_command_lists(commands) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": True})
        )
    ) as client:
        await TelegramClient("123:synthetic", client=client).set_commands(commands)


@pytest.mark.parametrize(
    "commands",
    [
        None,
        (),
        COMMANDS * 101,
        [None],
        [{"command": "connect"}],
        [{"command": "connect", "description": "Synthetic", "url": "https://opaque.invalid"}],
        [{"command": "https://opaque.invalid/synthetic-secret", "description": "Synthetic"}],
        [{"command": "CONNECT", "description": "Synthetic"}],
        [{"command": "", "description": "Synthetic"}],
        [{"command": "x" * 33, "description": "Synthetic"}],
        [{"command": "connect\n", "description": "Synthetic"}],
        [{"command": None, "description": "Synthetic"}],
        [{"command": "connect", "description": ""}],
        [{"command": "connect", "description": "  "}],
        [{"command": "connect", "description": "Synthetic\nsecret"}],
        [{"command": "connect", "description": "Synthetic\x00secret"}],
        [{"command": "connect", "description": "Synthetic\u202esecret"}],
        [{"command": "connect", "description": "Synthetic\ud800secret"}],
        [{"command": "connect", "description": "x" * 257}],
        [{"command": "connect", "description": "🙂" * 129}],
        [{"command": "connect", "description": None}],
        COMMANDS * 2,
    ],
)
async def test_invalid_commands_fail_before_network_without_echoing_content(commands) -> None:
    def api(request):
        pytest.fail("Invalid command menus must not reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        with pytest.raises(ValueError) as caught:
            await TelegramClient("123:synthetic", client=client).set_commands(commands)
    assert str(caught.value) == "Telegram command list is invalid."


@pytest.mark.parametrize("chat_id", [0, -7001, True, 2**63, "7001", 7001.0])
async def test_command_scope_requires_positive_private_chat_id(chat_id) -> None:
    def api(request):
        pytest.fail("Invalid command scopes must not reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        with pytest.raises(ValueError, match="command chat identifier is invalid"):
            await TelegramClient("123:synthetic", client=client).set_commands(
                COMMANDS, chat_id=chat_id
            )


@pytest.mark.parametrize("result", [False, None, 1, {}, "true"])
async def test_command_menu_requires_true_acknowledgement(result) -> None:
    calls = []

    def api(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        with pytest.raises(TelegramAPIError) as caught:
            await TelegramClient("123:synthetic", client=client).set_commands(COMMANDS)
    assert caught.value.code == 502
    assert len(calls) == 1


async def test_command_menu_transport_failure_is_safe_and_not_retried() -> None:
    calls = []

    def api(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic-secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        with pytest.raises(TelegramTransportError) as caught:
            await TelegramClient("123:synthetic", client=client).set_commands(COMMANDS)
    assert "synthetic-secret" not in str(caught.value)
    assert len(calls) == 1
