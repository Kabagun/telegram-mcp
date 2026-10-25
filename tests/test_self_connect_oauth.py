import base64
import hashlib
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from telegram_ping_mcp.config import Config
from telegram_ping_mcp.server import Runtime, create_http_app

BASE = "https://connector.example.test"
RESOURCE = BASE + "/mcp"
CALLBACK = "https://client.example.test/callback"
VERIFIER = "S" * 43


class SyntheticBot:
    def __init__(self):
        self.messages = []

    async def get_me(self):
        return {"username": "synthetic_bot"}

    async def set_commands(self, commands, *, chat_id=None):
        return None

    async def send_message(self, chat_id, text, **options):
        self.messages.append({"chat_id": chat_id, "text": text, **options})
        return {"message_id": len(self.messages), "chat": {"id": chat_id}}

    async def send_connector_icon(self, chat_id):
        return None

    async def aclose(self):
        return None


def command(text):
    return {
        "message": {
            "text": text,
            "chat": {"id": 7001, "type": "private"},
            "from": {"id": 7001, "is_bot": False, "first_name": "Synthetic"},
        }
    }


async def test_first_connect_code_completes_oauth_and_stays_scoped_after_reenrollment(tmp_path):
    config = Config(
        bot_token="123:synthetic-token",
        auth_token="synthetic-operator-token-for-oauth-test-123456",
        database_path=tmp_path / "state.sqlite3",
        polling_enabled=False,
        allowed_hosts=("connector.example.test",),
        public_mcp_url=RESOURCE,
    )
    bot = SyntheticBot()
    runtime = Runtime(config, bot=bot)
    app = create_http_app(runtime)
    async with app.router.lifespan_context(app):
        assert runtime.service.find_users() == []
        await runtime.handle_update(command("/connect"))
        user = runtime.service.store.get_user_by_telegram(7001)
        assert user["active"] is True and user["chat_id"] == 7001
        guide = bot.messages[-1]
        assert "приглашени" not in guide["text"].casefold()
        login = re.search(r"Код входа: ([A-Z2-7-]+)", guide["text"])[1]
        buttons = [item for row in guide["reply_markup"]["inline_keyboard"] for item in row]
        assert any(item.get("copy_text", {}).get("text") == login for item in buttons)
        with sqlite3.connect(runtime.oauth.database_path) as database:
            assert login not in "\n".join(database.iterdump())

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE, follow_redirects=False
        ) as http:
            registration = await http.post(
                "/register",
                json={
                    "redirect_uris": [CALLBACK],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "client_name": "Synthetic AI client",
                },
            )
            assert registration.status_code == 201
            client_id = registration.json()["client_id"]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
            response = await http.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": CALLBACK,
                    "response_type": "code",
                    "code_challenge": challenge.decode().rstrip("="),
                    "code_challenge_method": "S256",
                    "scope": "telegram",
                    "resource": RESOURCE,
                    "state": "synthetic-state",
                },
            )
            assert response.status_code == 302
            location = response.headers["location"]
            nonce = parse_qs(urlsplit(location).query)["request"][0]
            page = await http.get(location)
            assert page.status_code == 200
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
            approval = await http.post(
                "/oauth/consent",
                data={"request": nonce, "csrf": csrf, "code": login, "decision": "allow"},
                headers={"Origin": BASE},
            )
            assert approval.status_code == 302
            callback = parse_qs(urlsplit(approval.headers["location"]).query)
            assert callback["state"] == ["synthetic-state"]
            response = await http.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "redirect_uri": CALLBACK,
                    "code": callback["code"][0],
                    "code_verifier": VERIFIER,
                    "resource": RESOURCE,
                },
            )
            assert response.status_code == 200
            access_token = response.json()["access_token"]
        grant = await runtime.oauth.load_access_token(access_token)
        assert grant.user_key == user["user_key"] and grant.telegram_user_id == 7001
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + access_token},
        ) as http:
            async with streamable_http_client(RESOURCE, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool("find_users", {})
                    assert not result.isError
                    assert result.structuredContent["result"] == [user]
                    denied = await session.call_tool("get_user", {"user_key": "foreign-user"})
                    assert denied.isError
                    denied = await session.call_tool(
                        "create_user_link", {"user_key": "foreign-user", "display_name": "Other"}
                    )
                    assert denied.isError
        await runtime.handle_update(command("/unlink"))
        assert await runtime.oauth.load_access_token(access_token) is None
        await runtime.handle_update(command("/connect"))
        new_user = runtime.service.store.get_user_by_telegram(7001)
        assert new_user["user_key"] != user["user_key"]
        assert "Код входа: " in bot.messages[-1]["text"]
        assert await runtime.oauth.load_access_token(access_token) is None
