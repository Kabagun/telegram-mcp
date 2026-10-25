import asyncio
import base64
import json
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull

from telegram_ping_mcp.config import Config
from telegram_ping_mcp.server import Runtime, create_http_app
from telegram_ping_mcp.telegram import TelegramClient

BASE_URL = "https://connector.example"
MASTER = "synthetic-master-token-for-http-tests-123456"
ALPHA = "synthetic-alpha"
BETA = "synthetic-beta"
GROUP = -100123
FILE_BYTES = b"Synthetic fixture attachment"
TOOLS = {
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


class SyntheticTelegram:
    def __init__(self):
        self.calls = []
        self.sends = []
        self.downloads = 0

    def __call__(self, request):
        if "/file/" in request.url.path:
            self.downloads += 1
            self.calls.append(("download", {}))
            return httpx.Response(200, content=FILE_BYTES)
        method = request.url.path.rsplit("/", 1)[-1]
        arguments = json.loads(request.content)
        self.calls.append((method, arguments))
        if method == "getMe":
            result = {"id": 123, "is_bot": True, "username": "synthetic_bot"}
        elif method == "setMyCommands":
            result = True
        elif method == "getFile":
            result = {
                "file_id": arguments["file_id"],
                "file_path": "documents/fixture.txt",
                "file_size": len(FILE_BYTES),
            }
        elif method in {"sendMessage", "sendDocument"}:
            self.sends.append(arguments)
            result = {"message_id": len(self.sends), "chat": {"id": arguments["chat_id"]}}
        else:
            raise AssertionError("Unexpected Telegram API method")
        return httpx.Response(200, json={"ok": True, "result": result})


def payload(result):
    assert not result.isError, result.content
    value = result.structuredContent
    value = value if value is not None else json.loads(result.content[0].text)
    return value["result"] if isinstance(value, dict) and set(value) == {"result"} else value


def update(user_id, message_id, *, group=False):
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "chat": {
                "id": GROUP if group else user_id,
                "type": "supergroup" if group else "private",
            },
            "from": {"id": user_id, "is_bot": False},
            "caption": "Synthetic fixture caption",
            "document": {
                "file_id": f"synthetic-file-{message_id}",
                "file_name": "fixture.txt",
                "mime_type": "text/plain",
                "file_size": len(FILE_BYTES),
            },
        },
    }


@asynccontextmanager
async def running_server(tmp_path, *, oauth_enabled=True):
    config = Config(
        bot_token="123:synthetic",
        auth_token=MASTER,
        database_path=tmp_path / "server.sqlite3",
        polling_enabled=False,
        allowed_group_chat_ids=frozenset({GROUP}),
        allowed_hosts=("connector.example",),
        public_mcp_url=BASE_URL + "/mcp" if oauth_enabled else "",
    )
    telegram = SyntheticTelegram()
    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as bot_http:
        runtime = Runtime(config, bot=TelegramClient(config.bot_token, client=bot_http))
        app = create_http_app(runtime)
        async with app.router.lifespan_context(app):
            store = runtime.service.store
            for user_key, user_id in ((ALPHA, 7001), (BETA, 7002)):
                link = store.create_link(user_key, user_key)
                store.consume_link(link["token"], user_id, user_id, None, user_key)
            for user_id, message_id, group in (
                (7001, 11, False),
                (7002, 12, False),
                (7001, 13, True),
            ):
                await runtime.handle_update(update(user_id, message_id, group=group))
            messages = runtime.inbox.list_messages()
            tokens = {"legacy": MASTER}
            if runtime.oauth is not None:
                client = OAuthClientInformationFull(
                    client_id="synthetic-test-client",
                    client_name="Synthetic integration fixture",
                    redirect_uris=["https://client.example/callback"],
                    token_endpoint_auth_method="none",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                )
                await runtime.oauth.register_client(client)
                # Seed real grants here; protocol and consent have dedicated provider tests.
                for name, user_key, user_id in (("alpha", ALPHA, 7001), ("beta", BETA, 7002)):
                    with runtime.oauth._db() as database:
                        token = runtime.oauth._issue(
                            database, client.client_id, user_key, user_id, ["telegram"]
                        )
                    tokens[name] = token.access_token
            assert len(messages) == 3
            telegram.calls.clear()
            yield SimpleNamespace(
                runtime=runtime,
                app=app,
                telegram=telegram,
                tokens=tokens,
                own=next(item for item in messages if item["chat_id"] == 7001),
                foreign=next(item for item in messages if item["chat_id"] == 7002),
                group=next(item for item in messages if item["chat_id"] == GROUP),
            )


@pytest.fixture
def server(tmp_path):
    # AnyIO lifespan scopes must enter and exit in the same test task.
    return lambda: running_server(tmp_path)


@asynccontextmanager
async def connected(fixture, name):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fixture.app),
        base_url=BASE_URL,
        headers={"Authorization": "Bearer " + fixture.tokens[name]},
    ) as http:
        async with streamable_http_client(BASE_URL + "/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                await session.list_tools()
                yield session


def link_count(fixture):
    with sqlite3.connect(fixture.runtime.config.database_path) as database:
        return database.execute("SELECT COUNT(*) FROM link_tokens").fetchone()[0]


async def test_oauth_tools_reject_foreign_and_group_resources_before_side_effects(server):
    async with server() as server:
        async with AsyncExitStack() as stack:
            alpha = await stack.enter_async_context(connected(server, "alpha"))
            beta = await stack.enter_async_context(connected(server, "beta"))
            assert {tool.name for tool in (await alpha.list_tools()).tools} == TOOLS
            denied = [
                ("create_user_link", {"user_key": "blocked-third", "display_name": "Synthetic"}),
                (
                    "ping_group",
                    {
                        "chat_id": GROUP,
                        "user_keys": [ALPHA],
                        "text": "Synthetic",
                        "request_id": "group",
                    },
                ),
                ("get_user", {"user_key": BETA}),
                ("unlink_user", {"user_key": BETA}),
                (
                    "ping_user",
                    {"user_key": BETA, "text": "Synthetic", "request_id": "foreign-ping"},
                ),
                ("list_messages", {"user_key": BETA}),
                ("list_messages", {"chat_id": 7002}),
                ("list_messages", {"chat_id": GROUP}),
            ]
            for message in (server.foreign, server.group, {"id": 999999}):
                denied.extend(
                    [
                        ("get_message", {"message_id": message["id"]}),
                        ("mark_read", {"message_id": message["id"]}),
                        (
                            "reply_message",
                            {
                                "message_id": message["id"],
                                "text": "Synthetic",
                                "request_id": f"blocked-reply-{message['id']}",
                            },
                        ),
                        (
                            "reply_attachment",
                            {
                                "message_id": message["id"],
                                "request_id": f"blocked-file-{message['id']}",
                            },
                        ),
                    ]
                )
            for message in (server.foreign, server.group):
                denied.append(("get_attachment", {"attachment_id": message["attachment"]["id"]}))
            denied.append(("get_attachment", {"attachment_id": "z" * 32}))
            initial_links = link_count(server)
            for name, arguments in denied:
                result = await alpha.call_tool(name, arguments)
                assert result.isError, (name, arguments)
                expected = (
                    "operator access"
                    if name in {"create_user_link", "ping_group"}
                    else "Access denied."
                )
                assert expected in result.content[0].text
            assert server.telegram.calls == []
            assert link_count(server) == initial_links
            assert server.runtime.service.get_user(BETA) is not None
            assert not server.runtime.inbox.get_message(server.foreign["id"])["read"]
            assert not server.runtime.inbox.get_message(server.group["id"])["read"]

            own_lists = await asyncio.gather(
                alpha.call_tool("list_messages", {}), beta.call_tool("list_messages", {})
            )
            assert [item["id"] for item in payload(own_lists[0])] == [server.own["id"]]
            assert [item["id"] for item in payload(own_lists[1])] == [server.foreign["id"]]
            assert [
                item["user_key"] for item in payload(await alpha.call_tool("find_users", {}))
            ] == [ALPHA]
            assert payload(await alpha.call_tool("find_users", {"query": BETA})) == []
            assert [
                item["user_key"] for item in payload(await beta.call_tool("find_users", {}))
            ] == [BETA]
            assert (
                payload(await alpha.call_tool("get_user", {"user_key": ALPHA}))["telegram_user_id"]
                == 7001
            )
            assert (
                payload(await alpha.call_tool("get_message", {"message_id": server.own["id"]}))[
                    "id"
                ]
                == server.own["id"]
            )
            assert payload(await alpha.call_tool("mark_read", {"message_id": server.own["id"]}))[
                "read"
            ]
            assert (
                payload(
                    await alpha.call_tool(
                        "ping_user",
                        {"user_key": ALPHA, "text": "Synthetic ping", "request_id": "own-ping"},
                    )
                )["status"]
                == "sent"
            )
            assert (
                payload(
                    await alpha.call_tool(
                        "reply_message",
                        {
                            "message_id": server.own["id"],
                            "text": "Synthetic reply",
                            "request_id": "own-reply",
                        },
                    )
                )["status"]
                == "sent"
            )
            assert (
                payload(
                    await alpha.call_tool(
                        "reply_attachment",
                        {"message_id": server.own["id"], "request_id": "own-file"},
                    )
                )["status"]
                == "sent"
            )
            result = await alpha.call_tool(
                "get_attachment", {"attachment_id": server.own["attachment"]["id"]}
            )
            assert not result.isError
            resource = next(block.resource for block in result.content if block.type == "resource")
            assert base64.b64decode(resource.blob) == FILE_BYTES
            assert len(server.telegram.sends) == 3
            assert all(arguments["chat_id"] == 7001 for arguments in server.telegram.sends)


async def test_oauth_delivery_request_ids_are_independent_per_user(server):
    async with server() as server:
        async with AsyncExitStack() as stack:
            alpha = await stack.enter_async_context(connected(server, "alpha"))
            beta = await stack.enter_async_context(connected(server, "beta"))
            request_id = "synthetic-shared-request-id"
            arguments = {"text": "Synthetic shared text", "request_id": request_id}
            originals = await asyncio.gather(
                alpha.call_tool("ping_user", {**arguments, "user_key": ALPHA}),
                beta.call_tool("ping_user", {**arguments, "user_key": BETA}),
            )
            for result in originals:
                sent = payload(result)
                assert sent["status"] == "sent"
                assert sent["request_id"] == request_id
                assert not sent["duplicate"]
            assert sorted(arguments["chat_id"] for arguments in server.telegram.sends) == [
                7001,
                7002,
            ]
            repeated = await asyncio.gather(
                alpha.call_tool("ping_user", {**arguments, "user_key": ALPHA}),
                beta.call_tool("ping_user", {**arguments, "user_key": BETA}),
            )
            for original, result in zip(originals, repeated, strict=True):
                duplicate = payload(result)
                assert duplicate["status"] == "sent"
                assert duplicate["request_id"] == request_id
                assert duplicate["duplicate"]
                assert duplicate["message_id"] == payload(original)["message_id"]
            assert len(server.telegram.sends) == 2


async def test_legacy_master_retains_all_tools_and_group_access(server):
    async with server() as server:
        async with connected(server, "legacy") as session:
            assert (
                payload(
                    await session.call_tool(
                        "create_user_link", {"user_key": "synthetic-third", "display_name": "Third"}
                    )
                )["user_key"]
                == "synthetic-third"
            )
            assert {
                item["user_key"] for item in payload(await session.call_tool("find_users", {}))
            } == {ALPHA, BETA}
            assert (
                payload(await session.call_tool("get_user", {"user_key": BETA}))["telegram_user_id"]
                == 7002
            )
            assert len(payload(await session.call_tool("list_messages", {}))) == 3
            assert (
                payload(await session.call_tool("get_message", {"message_id": server.group["id"]}))[
                    "chat_id"
                ]
                == GROUP
            )
            assert payload(
                await session.call_tool("mark_read", {"message_id": server.group["id"]})
            )["read"]
            sends = [
                ("ping_user", {"user_key": BETA, "text": "Synthetic", "request_id": "legacy-ping"}),
                (
                    "ping_group",
                    {
                        "chat_id": GROUP,
                        "user_keys": [ALPHA, BETA],
                        "text": "Synthetic",
                        "request_id": "legacy-group",
                    },
                ),
                (
                    "reply_message",
                    {
                        "message_id": server.group["id"],
                        "text": "Synthetic",
                        "request_id": "legacy-reply",
                    },
                ),
                (
                    "reply_attachment",
                    {"message_id": server.group["id"], "request_id": "legacy-file"},
                ),
            ]
            for name, arguments in sends:
                assert payload(await session.call_tool(name, arguments))["status"] == "sent"
            result = await session.call_tool(
                "get_attachment", {"attachment_id": server.foreign["attachment"]["id"]}
            )
            assert not result.isError
            assert [arguments["chat_id"] for arguments in server.telegram.sends] == [
                7002,
                GROUP,
                GROUP,
                GROUP,
            ]
            assert payload(await session.call_tool("unlink_user", {"user_key": BETA}))["unlinked"]
            assert await server.runtime.oauth.load_access_token(server.tokens["beta"]) is None


async def test_public_discovery_challenges_and_attachment_http_ownership(server):
    async with server() as server:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url=BASE_URL
        ) as http:
            metadata_path = "/.well-known/oauth-protected-resource/mcp"
            response = await http.get(metadata_path)
            assert response.status_code == 200
            assert response.json()["resource"] == BASE_URL + "/mcp"
            assert response.json()["authorization_servers"] == [BASE_URL + "/"]
            response = await http.get("/.well-known/oauth-authorization-server")
            assert response.status_code == 200
            assert response.json()["issuer"].rstrip("/") == BASE_URL
            assert (
                await http.get(metadata_path, headers={"Host": "attacker.invalid"})
            ).status_code == 421
            path = "/attachments/" + server.own["attachment"]["id"]
            for protected_path in ("/mcp", path):
                for headers in (
                    {},
                    {"Authorization": "Bearer wrong"},
                    [("Authorization", "Bearer " + MASTER), ("Authorization", "Bearer wrong")],
                ):
                    response = await http.get(protected_path, headers=headers)
                    assert response.status_code == 401
                    assert (
                        f'resource_metadata="{BASE_URL}{metadata_path}"'
                        in response.headers["www-authenticate"]
                    )
                    assert response.headers["cache-control"] == "no-store"
            for token, item in (
                (server.tokens["beta"], server.own),
                (server.tokens["alpha"], server.foreign),
                (server.tokens["alpha"], server.group),
            ):
                response = await http.get(
                    "/attachments/" + item["attachment"]["id"],
                    headers={"Authorization": "Bearer " + token},
                )
                assert response.status_code == 403
                assert response.json() == {"error": "Access denied."}
            assert server.telegram.calls == []
            for token, item in ((server.tokens["alpha"], server.own), (MASTER, server.group)):
                response = await http.get(
                    "/attachments/" + item["attachment"]["id"],
                    headers={"Authorization": "Bearer " + token},
                )
                assert response.status_code == 200
                assert response.content == FILE_BYTES
                assert response.headers["cache-control"] == "no-store"
            assert server.telegram.downloads == 2
            assert (
                await http.get(
                    path, headers={"Authorization": "Bearer " + MASTER, "Host": "attacker.invalid"}
                )
            ).status_code == 421
            server.runtime.oauth.revoke_user(ALPHA)
            response = await http.get(
                path, headers={"Authorization": "Bearer " + server.tokens["alpha"]}
            )
            assert response.status_code == 401
            assert server.telegram.downloads == 2


async def test_oauth_unlink_own_user_revokes_only_their_grant(server):
    async with server() as server:
        async with connected(server, "alpha") as session:
            assert payload(await session.call_tool("unlink_user", {"user_key": ALPHA}))["unlinked"]
        assert server.runtime.service.get_user(ALPHA) is None
        assert server.runtime.service.get_user(BETA) is not None
        assert await server.runtime.oauth.load_access_token(server.tokens["alpha"]) is None
        assert await server.runtime.oauth.load_access_token(server.tokens["beta"]) is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url=BASE_URL
        ) as http:
            for path in ("/mcp", "/attachments/" + server.own["attachment"]["id"]):
                response = await http.get(
                    path, headers={"Authorization": "Bearer " + server.tokens["alpha"]}
                )
                assert response.status_code == 401
        async with connected(server, "beta") as session:
            assert [
                item["user_key"] for item in payload(await session.call_tool("find_users", {}))
            ] == [BETA]
        assert server.telegram.calls == []


async def test_without_public_url_retains_legacy_auth_without_oauth_metadata(tmp_path):
    async with running_server(tmp_path, oauth_enabled=False) as fixture:
        assert fixture.runtime.oauth is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fixture.app), base_url=BASE_URL
        ) as http:
            for path in ("/mcp", "/.well-known/oauth-protected-resource/mcp"):
                response = await http.get(path)
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == "Bearer"
        async with connected(fixture, "legacy") as session:
            assert len(payload(await session.call_tool("find_users", {}))) == 2
            assert len(payload(await session.call_tool("list_messages", {}))) == 3
