import asyncio
import json
import sys
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from telegram_ping_mcp.config import Config
from telegram_ping_mcp.server import PollingLock, Runtime, StartupError, create_http_app, create_mcp
from telegram_ping_mcp.telegram import TelegramClient

TOKEN = "123456:synthetic-token"
AUTH = "synthetic-auth-token-0123456789-abcdef"


@pytest.fixture
def config(tmp_path):
    return Config(
        bot_token=TOKEN,
        auth_token=AUTH,
        database_path=tmp_path / "test.sqlite3",
        allowed_group_chat_ids=frozenset({-100123}),
        polling_enabled=False,
    )


class FakeTelegram:
    def __init__(self):
        self.sends = []
        self.command_menus = []
        self.updates = []
        self.polled = asyncio.Event()
        self.polling_error = False

    async def __call__(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        data = json.loads(request.content)
        if method == "getMe":
            result = {"id": 123456, "is_bot": True, "username": "synthetic_test_bot"}
        elif method == "setMyCommands":
            self.command_menus.append(data)
            result = True
        elif method == "sendMessage":
            self.sends.append(data)
            result = {"message_id": len(self.sends), "chat": {"id": data["chat_id"]}}
        elif method == "getUpdates":
            self.polled.set()
            if self.polling_error:
                return httpx.Response(409, json={"ok": False, "error_code": 409})
            if self.updates:
                result, self.updates = self.updates, []
            else:
                await asyncio.Event().wait()
        else:
            raise AssertionError("Unexpected bot API call")
        return httpx.Response(200, json={"ok": True, "result": result})


def private_command(text, user_id=7001, update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"type": "private", "id": user_id},
            "from": {"id": user_id, "is_bot": False, "first_name": "Synthetic"},
            "text": text,
        },
    }


def structured(result):
    assert not result.isError, result.content
    data = result.structuredContent
    data = data if data is not None else json.loads(result.content[0].text)
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


async def test_real_sdk_http_end_to_end_auth_link_send_dedup_optout_group(config):
    telegram = FakeTelegram()
    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as bot_http:
        runtime = Runtime(config, bot=TelegramClient(TOKEN, client=bot_http))
        app = create_http_app(runtime)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                assert (await client.get("/health")).json() == {
                    "status": "ready",
                    "polling": "disabled",
                }
                for method in ("GET", "POST", "DELETE", "OPTIONS"):
                    assert (await client.request(method, "/mcp")).status_code == 401
                assert (await client.post("/mcp/anything")).status_code == 401
                assert (
                    await client.post("/mcp", headers={"Authorization": "Bearer wrong"})
                ).status_code == 401
                response = await client.post(
                    "/mcp",
                    headers={"Authorization": f"Bearer {AUTH}", "Host": "attacker.invalid"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                assert response.status_code == 421
                response = await client.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {AUTH}",
                        "Origin": "https://attacker.invalid",
                    },
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                assert response.status_code == 403
            async with httpx.AsyncClient(
                transport=transport, headers={"Authorization": f"Bearer {AUTH}"}
            ) as client:
                async with streamable_http_client(
                    "http://127.0.0.1/mcp", http_client=client
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        initialized = await session.initialize()
                        assert initialized.serverInfo.name == "Telegram Ping MCP"
                        tools = await session.list_tools()
                        assert {tool.name for tool in tools.tools} == {
                            "create_user_link",
                            "find_users",
                            "get_user",
                            "unlink_user",
                            "ping_user",
                            "ping_group",
                            "list_messages",
                            "get_message",
                            "get_attachment",
                            "reply_message",
                            "reply_attachment",
                            "mark_read",
                        }
                        invitation = structured(
                            await session.call_tool(
                                "create_user_link",
                                {"user_key": "synthetic", "display_name": "Test 🛰️"},
                            )
                        )
                        token = parse_qs(urlparse(invitation["link"]).query)["start"][0]
                        await runtime.service.handle_update(private_command(f"/start {token}"))
                        user = structured(
                            await session.call_tool("get_user", {"user_key": "synthetic"})
                        )
                        assert user["telegram_user_id"] == 7001
                        params = {
                            "user_key": "synthetic",
                            "text": "Synthetic notification",
                            "request_id": "demo-1",
                        }
                        sent = structured(await session.call_tool("ping_user", params))
                        again = structured(await session.call_tool("ping_user", params))
                        assert sent["status"] == again["status"] == "sent"
                        assert sent["message_id"] == again["message_id"]
                        assert again["duplicate"] is True
                        assert len(telegram.sends) == 2  # command reply and notification
                        changed = await session.call_tool(
                            "ping_user", {**params, "text": "changed"}
                        )
                        assert changed.isError
                        group = structured(
                            await session.call_tool(
                                "ping_group",
                                {
                                    "chat_id": -100123,
                                    "user_keys": ["synthetic"],
                                    "text": "🛰️ hello",
                                    "request_id": "demo-group",
                                    "topic_id": 9,
                                },
                            )
                        )
                        assert group["status"] == "sent"
                        payload = telegram.sends[-1]
                        entity = payload["entities"][0]
                        mentioned = (
                            payload["text"]
                            .encode("utf-16-le")[
                                entity["offset"] * 2 : (entity["offset"] + entity["length"]) * 2
                            ]
                            .decode("utf-16-le")
                        )
                        assert mentioned == "Test 🛰️"
                        assert entity["user"]["id"] == 7001
                        assert payload["message_thread_id"] == 9
                        assert "parse_mode" not in payload
                        await runtime.service.handle_update(private_command("/stop", update_id=2))
                        before = len(telegram.sends)
                        blocked = await session.call_tool(
                            "ping_user", {**params, "request_id": "demo-2"}
                        )
                        assert blocked.isError
                        assert len(telegram.sends) == before
                        assert structured(
                            await session.call_tool("unlink_user", {"user_key": "synthetic"})
                        )["unlinked"]
        assert runtime.readiness()["status"] == "unavailable"


async def test_official_memory_transport_tools(config):
    telegram = FakeTelegram()
    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
        runtime = Runtime(config, bot=TelegramClient(TOKEN, client=client))
        async with runtime.lifespan():
            async with create_connected_server_and_client_session(create_mcp(runtime)) as session:
                assert len((await session.list_tools()).tools) == 12
                assert not (await session.call_tool("find_users", {})).isError


async def test_polling_fatal_marks_health_unready_and_shutdown_releases_lock(config):
    telegram = FakeTelegram()
    telegram.polling_error = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
        runtime = Runtime(
            replace(config, polling_enabled=True), bot=TelegramClient(TOKEN, client=client)
        )
        app = create_http_app(runtime)
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(telegram.polled.wait(), timeout=2)
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(runtime.polling_task, return_exceptions=True)),
                timeout=2,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as http:
                response = await http.get("/health")
                assert response.status_code == 503
                assert response.json() == {"status": "unavailable", "polling": "failed"}
            with pytest.raises(ValueError, match="not ready"):
                runtime.require_service()
        lock = PollingLock(config.database_path)
        lock.acquire()
        lock.release()


async def test_polling_updates_link_and_cancel_cleanly(config):
    telegram = FakeTelegram()
    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
        runtime = Runtime(
            replace(config, polling_enabled=True), bot=TelegramClient(TOKEN, client=client)
        )
        async with runtime.lifespan():
            link = runtime.service.create_user_link("synthetic", "Test")
            token = parse_qs(urlparse(link["link"]).query)["start"][0]
            telegram.updates.append(private_command(f"/start {token}"))
            await asyncio.wait_for(telegram.polled.wait(), timeout=2)
            # Bot mock calls and SQLite work complete without yielding in handler.
            for _ in range(50):
                if runtime.service.get_user("synthetic") is not None:
                    break
                await asyncio.sleep(0)
            assert runtime.service.get_user("synthetic")["active"]
            assert runtime.service.store.get_offset() == 2
            task = runtime.polling_task
        assert task.done()


def test_duplicate_polling_lock_and_release(config):
    first, second = PollingLock(config.database_path), PollingLock(config.database_path)
    first.acquire()
    try:
        with pytest.raises(StartupError, match="Another poller"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


async def test_real_stdio_subprocess_sdk_protocol_without_real_telegram(tmp_path):
    script = tmp_path / "fake_stdio.py"
    script.write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "from telegram_ping_mcp.__main__ import _stdio\n"
        "from telegram_ping_mcp.config import Config\n"
        "from telegram_ping_mcp.server import Runtime\n"
        "class Bot:\n"
        "    async def get_me(self):\n"
        "        return {'username': 'synthetic_test_bot'}\n"
        "    async def set_commands(self, commands, *, chat_id=None): pass\n"
        "    async def aclose(self): pass\n"
        "asyncio.run(_stdio(Runtime(Config(\n"
        "    bot_token='123:synthetic', polling_enabled=False,\n"
        "    database_path=Path(__file__).with_suffix('.sqlite3')), bot=Bot())))\n",
        encoding="utf-8",
    )
    parameters = StdioServerParameters(command=sys.executable, args=[str(script)])
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            assert (await session.initialize()).serverInfo.name == "Telegram Ping MCP"
            assert len((await session.list_tools()).tools) == 12
            assert structured(await session.call_tool("find_users", {})) == []


async def test_startup_failure_closes_bot_and_releases_polling_lock(config):
    class BrokenBot:
        closed = False

        async def get_me(self):
            raise StartupError("Synthetic startup failure")

        async def aclose(self):
            self.closed = True

    bot = BrokenBot()
    runtime = Runtime(replace(config, polling_enabled=True), bot=bot)
    with pytest.raises(StartupError, match="Synthetic startup"):
        async with runtime.lifespan():
            pytest.fail("startup should not complete")
    assert bot.closed
    assert runtime.readiness() == {"status": "unavailable", "polling": "stopped"}
    lock = PollingLock(config.database_path)
    lock.acquire()
    lock.release()
