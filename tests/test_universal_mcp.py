"""Real SDK coverage of the small tool interface with no Telegram network calls."""

import asyncio
import hashlib
import json
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from telegram_ping_mcp.config import Config
from telegram_ping_mcp.inbox import InboxStore
from telegram_ping_mcp.server import create_mcp
from telegram_ping_mcp.store import Store

ALPHA = {"user_key": "synthetic-alpha", "telegram_user_id": 7001}
BETA = {"user_key": "synthetic-beta", "telegram_user_id": 7002}
VISIBLE = {"get_profile", "get_profiles", "get_messages", "get_attachment", "send_message"}


def payload(result):
    assert not result.isError, result.content
    value = result.structuredContent
    value = value if value is not None else json.loads(result.content[0].text)
    return value["result"] if isinstance(value, dict) and set(value) == {"result"} else value


class CapturingSender:
    def __init__(self):
        self.calls = []

    async def send(self, **arguments):
        self.calls.append(arguments)
        await asyncio.sleep(0)
        return {"status": "sent", "request_id": arguments["request_id"], "message_id": 123}


class SyntheticPrincipalMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            identity = dict(scope["headers"]).get(b"x-synthetic-identity")
            if identity in {b"alpha", b"beta"}:
                scope["telegram_oauth_user"] = (ALPHA if identity == b"alpha" else BETA).copy()
            elif identity == b"malformed":
                scope["telegram_oauth_user"] = None
        await self.app(scope, receive, send)


@pytest.fixture
def fixture(tmp_path):
    config = Config(bot_token="123:synthetic", database_path=tmp_path / "state.sqlite3")
    store = Store(config.database_path)
    inbox = InboxStore(config.database_path)
    sender = CapturingSender()
    for identity, username, label in (
        (ALPHA, "synthetic_alpha", "Alpha connection"),
        (BETA, None, "Beta connection"),
    ):
        link = store.create_link(identity["user_key"], label)
        user_id = identity["telegram_user_id"]
        store.consume_link(link["token"], user_id, user_id, username, "Synthetic")
        for chat_id in (user_id, -100123):
            inbox._save(
                user_key=identity["user_key"],
                telegram_user_id=user_id,
                chat_id=chat_id,
                telegram_message_id=user_id,
                topic_id=9 if chat_id < 0 else None,
                sent_at=1000,
                text="Synthetic inbox fixture",
                caption=None,
                attachment={
                    "id": ("a" if user_id == 7001 else "b") * 31 + ("p" if chat_id > 0 else "g"),
                    "kind": "document",
                    "file_id": f"synthetic-file-{user_id}",
                    "name": "fixture.txt",
                    "mime_type": "text/plain",
                    "size": 12,
                },
            )
    service = SimpleNamespace(
        store=store,
        get_user=store.get_user,
        find_users=lambda query="", limit=50: store.list_users(query, limit),
    )
    runtime = SimpleNamespace(
        config=config,
        require_service=lambda: service,
        require_inbox=lambda: inbox,
        require_sender=lambda: sender,
    )
    mcp = create_mcp(runtime)
    app = mcp.streamable_http_app()
    app.add_middleware(SyntheticPrincipalMiddleware)
    messages = inbox.list_messages()
    return SimpleNamespace(
        mcp=mcp,
        app=app,
        store=store,
        inbox=inbox,
        sender=sender,
        own=next(item for item in messages if item["chat_id"] == 7001),
        foreign=next(item for item in messages if item["chat_id"] == 7002),
        group=next(
            item
            for item in messages
            if item["user_key"] == ALPHA["user_key"] and item["chat_id"] < 0
        ),
    )


@asynccontextmanager
async def sessions(fixture, *identities):
    async with fixture.app.router.lifespan_context(fixture.app), AsyncExitStack() as stack:
        connected = []
        for identity in identities:
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=fixture.app),
                    headers={"x-synthetic-identity": identity},
                )
            )
            streams = await stack.enter_async_context(
                streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http)
            )
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
            connected.append(session)
        yield connected


async def test_discovery_has_five_tools_and_typed_file_contract(fixture):
    async with create_connected_server_and_client_session(fixture.mcp) as session:
        initialized = await session.initialize()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == VISIBLE
        send = tools["send_message"]
        assert send.meta == {"openai/fileParams": ["file"]}
        assert set(send.inputSchema["required"]) == {"profile_id", "request_id"}
        file = send.inputSchema["$defs"]["OutgoingFile"]
        assert set(file["required"]) == {"download_url", "file_id"}
        assert set(file["properties"]) == {"download_url", "file_id", "mime_type", "file_name"}
        assert all(field["type"] == "string" for field in file["properties"].values())
        assert file["additionalProperties"] is False
        assert tools["get_profile"].meta == {
            "openai/profile": True,
            "ui": {"visibility": ["app"]},
            "openai/visibility": "private",
        }
        assert "ask which account" in initialized.instructions
        assert "Never choose the first" in initialized.instructions
        assert "several connected links" in initialized.instructions
        assert "Resolve an explicitly named account" in initialized.instructions
        assert "name or nickname only against get_profiles" in initialized.instructions
        assert "ask the user if no account or several accounts match" in initialized.instructions
        # Cached clients retain their previous call handler without rediscovering it.
        assert len(payload(await session.call_tool("find_users", {}))) == 2
        denied = await session.call_tool("send_message", {"request_id": "missing-profile"})
        assert denied.isError
        denied = await session.call_tool(
            "send_message",
            {"profile_id": "7001", "request_id": "invalid-file", "file": {"file_id": "file"}},
        )
        assert denied.isError
        for optional in ("mime_type", "file_name"):
            denied = await session.call_tool(
                "send_message",
                {
                    "profile_id": "7001",
                    "request_id": "invalid-null-file-metadata",
                    "file": {
                        "download_url": "https://files.example.test/synthetic",
                        "file_id": "synthetic-client-file",
                        optional: None,
                    },
                },
            )
            assert denied.isError
    assert fixture.sender.calls == []


async def test_send_accepts_file_with_omitted_optional_metadata(fixture):
    file = {
        "download_url": "https://files.example.test/synthetic",
        "file_id": "synthetic-client-file",
    }
    async with create_connected_server_and_client_session(fixture.mcp) as session:
        result = await session.call_tool(
            "send_message",
            {"profile_id": "7001", "request_id": "minimal-client-file", "file": file},
        )
        assert not result.isError
        assert fixture.sender.calls[0]["file"] == file
        assert set(fixture.sender.calls[0]["file"]) == {"download_url", "file_id"}


async def test_operator_profiles_are_minimal_and_explicit_selection_is_required(fixture):
    async with create_connected_server_and_client_session(fixture.mcp) as session:
        profiles = payload(await session.call_tool("get_profiles", {}))
        assert {profile["id"] for profile in profiles} == {"7001", "7002"}
        assert all(set(profile) == {"id", "name", "nickname"} for profile in profiles)
        ambiguous = await session.call_tool("get_messages", {})
        assert ambiguous.isError
        assert "Ask the user which account" in ambiguous.content[0].text
        for profile_id in ("synthetic-alpha", "@synthetic_alpha", "07001", "-100123", "7003"):
            result = await session.call_tool(
                "send_message", {"profile_id": profile_id, "request_id": "invalid", "text": "x"}
            )
            assert result.isError
        fixture.store.set_active_by_telegram(7002, False)
        assert payload(await session.call_tool("get_profiles", {})) == [
            {"id": "7001", "name": "@synthetic_alpha", "nickname": "@synthetic_alpha"}
        ]
        assert {item["user_key"] for item in payload(await session.call_tool("get_messages", {}))}
        assert payload(await session.call_tool("find_users", {}))[1]["active"] is False
    assert fixture.sender.calls == []


async def test_oauth_connections_scope_profiles_and_inbox_without_identity_leaks(fixture):
    async with sessions(fixture, "alpha", "beta", "legacy", "malformed") as connected:
        alpha, beta, legacy, malformed = connected
        result = await asyncio.gather(
            *(session.call_tool("get_profiles", {}) for session in (alpha, beta, legacy))
        )
        assert [{item["id"] for item in payload(value)} for value in result] == [
            {"7001"},
            {"7002"},
            {"7001", "7002"},
        ]
        assert payload(await alpha.call_tool("get_messages", {})) == [fixture.own]
        assert (
            payload(await alpha.call_tool("get_messages", {"message_id": fixture.own["id"]}))
            == fixture.own
        )
        for arguments in (
            {"profile_id": "7002"},
            {"message_id": fixture.foreign["id"]},
            {"message_id": fixture.group["id"]},
            {"message_id": 999999},
        ):
            denied = await alpha.call_tool("get_messages", arguments)
            assert denied.isError
            assert "Access denied." in denied.content[0].text
        assert (await malformed.call_tool("get_profiles", {})).isError
        assert (await alpha.call_tool("get_user", {"user_key": BETA["user_key"]})).isError
        fixture.store.set_active_by_telegram(7001, False)
        assert (await alpha.call_tool("get_profiles", {})).isError
        assert (await alpha.call_tool("get_profile", {})).isError
    assert fixture.sender.calls == []


async def test_send_delegates_text_file_reply_and_schedule_with_oauth_request_scope(fixture):
    async with sessions(fixture, "alpha", "beta") as (alpha, beta):
        file = {
            "download_url": "https://files.example.test/synthetic",
            "file_id": "synthetic-client-file",
            "mime_type": "text/plain",
            "file_name": "fixture.txt",
        }
        arguments = {
            "profile_id": "7001",
            "request_id": "synthetic-shared-request",
            "text": "Synthetic caption",
            "message_id": fixture.own["id"],
            "file": file,
            "send_at": "2030-01-01T12:00:00+03:00",
        }
        assert (
            payload(await alpha.call_tool("send_message", arguments))["request_id"]
            == arguments["request_id"]
        )
        digest = hashlib.sha256(
            json.dumps([ALPHA["user_key"], arguments["request_id"]], ensure_ascii=True).encode(
                "ascii"
            )
        ).hexdigest()
        assert fixture.sender.calls[0] == {
            "user_key": ALPHA["user_key"],
            "request_id": "oauth:" + digest,
            "text": arguments["text"],
            "message_id": fixture.own["id"],
            "attachment_id": None,
            "file": file,
            "send_at": arguments["send_at"],
            "delay_seconds": None,
        }
        await beta.call_tool(
            "send_message",
            {"profile_id": "7002", "request_id": arguments["request_id"], "text": "Synthetic"},
        )
        assert fixture.sender.calls[1]["request_id"] != fixture.sender.calls[0]["request_id"]
        assert fixture.sender.calls[1]["user_key"] == BETA["user_key"]
        result = await alpha.call_tool(
            "send_message",
            {
                "profile_id": "7001",
                "request_id": "stored",
                "delay_seconds": 300,
                "attachment_id": fixture.own["attachment"]["id"],
            },
        )
        assert not result.isError
        assert fixture.sender.calls[2]["delay_seconds"] == 300
        assert fixture.sender.calls[2]["attachment_id"] == fixture.own["attachment"]["id"]
        assert fixture.sender.calls[2]["text"] == ""


async def test_send_rejects_foreign_profiles_messages_and_attachments_before_delegating(fixture):
    async with sessions(fixture, "alpha") as (alpha,):
        denied = [{"profile_id": "7002"}, {"profile_id": "7001", "message_id": 999999}]
        for message in (fixture.foreign, fixture.group):
            denied.extend(
                [
                    {"profile_id": "7001", "message_id": message["id"]},
                    {"profile_id": "7001", "attachment_id": message["attachment"]["id"]},
                ]
            )
        for options in denied:
            result = await alpha.call_tool(
                "send_message", {"request_id": "denied", "text": "Synthetic", **options}
            )
            assert result.isError
            assert "Access denied." in result.content[0].text
    assert fixture.sender.calls == []
