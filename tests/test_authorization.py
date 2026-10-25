import asyncio
import json
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.requests import Request

from telegram_ping_mcp.authorization import (
    is_oauth,
    principal,
    require_message,
    require_private_chat,
    require_user,
)

ALPHA = {"user_key": "synthetic-alpha", "telegram_user_id": 7001}
BETA = {"user_key": "synthetic-beta", "telegram_user_id": 7002}
_ABSENT = object()


def request_with(identity=_ABSENT):
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
    if identity is not _ABSENT:
        scope["telegram_oauth_user"] = identity
    return Request(scope)


def structured(result):
    assert not result.isError, result.content
    value = result.structuredContent
    return value if value is not None else json.loads(result.content[0].text)


def test_oauth_principal_uses_live_sdk_request_and_returns_a_copy():
    request = request_with(ALPHA.copy())
    token = request_ctx.set(SimpleNamespace(request=request))
    try:
        assert principal() == ALPHA
        assert is_oauth()
        found = principal()
        found["user_key"] = "changed"
        assert principal() == ALPHA
        assert principal(request=request_with(BETA)) == BETA
    finally:
        request_ctx.reset(token)
    assert principal() is None


@pytest.mark.parametrize("http_request", [None, request_with()])
def test_legacy_transports_retain_access(http_request):
    assert principal(request=http_request) is None
    assert not is_oauth(request=http_request)
    require_user("any-verified-user", request=http_request)
    require_message({"user_key": "any-verified-user", "chat_id": -100123}, request=http_request)
    assert require_private_chat(None, request=http_request) is None
    assert require_private_chat(-100123, request=http_request) == -100123


@pytest.mark.parametrize(
    "identity",
    [
        None,
        False,
        "synthetic-alpha",
        {},
        {"user_key": "synthetic-alpha"},
        {"telegram_user_id": 7001},
        {"user_key": "", "telegram_user_id": 7001},
        {"user_key": 17, "telegram_user_id": 7001},
        {"user_key": "synthetic-alpha", "telegram_user_id": True},
        {"user_key": "synthetic-alpha", "telegram_user_id": "7001"},
        {"user_key": "synthetic-alpha", "telegram_user_id": 7001.0},
        {"user_key": "synthetic-alpha", "telegram_user_id": 0},
        {"user_key": "synthetic-alpha", "telegram_user_id": -100123},
        {"user_key": "synthetic-alpha", "telegram_user_id": 2**63},
    ],
)
def test_malformed_oauth_identity_fails_closed_for_every_helper(identity):
    request = request_with(identity)
    for callback in (
        lambda: principal(request=request),
        lambda: is_oauth(request=request),
        lambda: require_user(ALPHA["user_key"], request=request),
        lambda: require_private_chat(None, request=request),
        lambda: require_message({"user_key": ALPHA["user_key"], "chat_id": 7001}, request=request),
    ):
        with pytest.raises(ValueError, match=r"^Access denied\.$"):
            callback()


def test_oauth_checks_own_user_and_default_private_chat():
    request = request_with(ALPHA)
    require_user(ALPHA["user_key"], request=request)
    assert require_private_chat(None, request=request) == 7001
    assert require_private_chat(7001, request=request) == 7001
    require_message({"user_key": ALPHA["user_key"], "chat_id": 7001}, request=request)
    with pytest.raises(ValueError, match=r"^Access denied\.$"):
        require_user(BETA["user_key"], request=request)


@pytest.mark.parametrize("chat_id", [7002, -100123, 0, True, 7001.0, "7001"])
def test_oauth_private_chat_rejects_other_and_malformed_ids(chat_id):
    with pytest.raises(ValueError, match=r"^Access denied\.$"):
        require_private_chat(chat_id, request=request_with(ALPHA))


@pytest.mark.parametrize(
    "message",
    [
        None,
        {},
        {"user_key": BETA["user_key"], "chat_id": 7001},
        {"user_key": ALPHA["user_key"], "chat_id": 7002},
        {"user_key": ALPHA["user_key"], "chat_id": -100123},
        {"user_key": ALPHA["user_key"], "chat_id": True},
        {"user_key": ALPHA["user_key"], "chat_id": 7001.0},
        {"user_key": ALPHA["user_key"], "chat_id": "7001"},
    ],
)
def test_oauth_message_requires_own_user_and_private_chat(message):
    with pytest.raises(ValueError, match=r"^Access denied\.$"):
        require_message(message, request=request_with(ALPHA))


def test_missing_message_is_denied_without_revealing_existence():
    with pytest.raises(ValueError, match=r"^Access denied\.$"):
        require_message(None)


class SyntheticPrincipalMiddleware:
    """Inject only synthetic identities, as the real auth middleware will."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            authorization = dict(scope["headers"]).get(b"authorization")
            identities = {b"Bearer alpha": ALPHA, b"Bearer beta": BETA}
            if authorization in identities:
                scope["telegram_oauth_user"] = identities[authorization].copy()
            elif authorization == b"Bearer malformed":
                scope["telegram_oauth_user"] = None
        await self.app(scope, receive, send)


async def test_real_sdk_http_concurrent_principals_do_not_leak_to_legacy_or_memory():
    mcp = FastMCP("Authorization fixture", stateless_http=True, json_response=True)
    arrived = set()
    all_arrived = asyncio.Event()

    @mcp.tool()
    async def identity_probe(tag: str, overlap: bool = False) -> dict:
        before = principal()
        if overlap:
            arrived.add(tag)
            if len(arrived) == 3:
                all_arrived.set()
            await asyncio.wait_for(all_arrived.wait(), timeout=5)
        await asyncio.sleep(0)
        return {
            "before": before,
            "after": principal(),
            "oauth": is_oauth(),
            "private_chat": require_private_chat(None),
        }

    @mcp.tool()
    def own_user(user_key: str) -> dict:
        require_user(user_key)
        return {"allowed": True}

    app = mcp.streamable_http_app()
    app.add_middleware(SyntheticPrincipalMiddleware)
    async with app.router.lifespan_context(app):
        async with AsyncExitStack() as stack:
            sessions = []
            for name in ("alpha", "beta", "legacy", "malformed"):
                http = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        headers={"Authorization": f"Bearer {name}"},
                    )
                )
                streams = await stack.enter_async_context(
                    streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http)
                )
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
                sessions.append(session)

            results = await asyncio.gather(
                *(
                    session.call_tool("identity_probe", {"tag": tag, "overlap": True})
                    for session, tag in zip(sessions[:3], ("alpha", "beta", "legacy"), strict=True)
                )
            )
            for result, expected in zip(results, (ALPHA, BETA, None), strict=True):
                assert structured(result) == {
                    "before": expected,
                    "after": expected,
                    "oauth": expected is not None,
                    "private_chat": expected["telegram_user_id"] if expected is not None else None,
                }
            assert arrived == {"alpha", "beta", "legacy"}
            assert not (
                await sessions[0].call_tool("own_user", {"user_key": ALPHA["user_key"]})
            ).isError
            foreign = await sessions[0].call_tool("own_user", {"user_key": BETA["user_key"]})
            assert foreign.isError
            assert "Access denied." in foreign.content[0].text
            assert not (
                await sessions[2].call_tool("own_user", {"user_key": BETA["user_key"]})
            ).isError
            malformed = await sessions[3].call_tool("identity_probe", {"tag": "malformed"})
            assert malformed.isError
            assert "Access denied." in malformed.content[0].text
            # Repeat after an OAuth call to catch identity retained across requests.
            assert (
                structured(await sessions[2].call_tool("identity_probe", {"tag": "legacy-after"}))[
                    "before"
                ]
                is None
            )

    async with create_connected_server_and_client_session(mcp) as session:
        assert structured(await session.call_tool("identity_probe", {"tag": "memory"})) == {
            "before": None,
            "after": None,
            "oauth": False,
            "private_chat": None,
        }
    assert principal() is None
