import base64
import hashlib
import json
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
        self.icons = []
        self.callback_answers = []
        self.deleted = []

    async def get_me(self):
        return {"username": "synthetic_bot"}

    async def set_commands(self, commands, *, chat_id=None):
        return None

    async def send_message(self, chat_id, text, **options):
        self.messages.append({"chat_id": chat_id, "text": text, **options})
        return {"message_id": len(self.messages), "chat": {"id": chat_id}}

    async def send_connector_icon(self, chat_id, *, reply_markup=None):
        self.icons.append({"chat_id": chat_id, "reply_markup": reply_markup})
        return None

    async def answer_callback_query(self, query_id, text=None):
        self.callback_answers.append(query_id)
        return None

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

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
            assert approval.status_code == 200
            assert "Как назвать аккаунт?" in approval.text
            approval = await http.post(
                "/oauth/consent",
                data={
                    "request": nonce,
                    "csrf": csrf,
                    "code": login,
                    "decision": "allow",
                    "account_name": "Synthetic",
                },
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
        await runtime.handle_update(command("🔌 Подключить AI-приложение"))
        assert "Куда подключаем Telegram?" == bot.messages[-1]["text"]
        await runtime.handle_update(command("/connect"))
        next_login = re.search(r"Код входа: ([A-Z2-7-]+)", bot.messages[-1]["text"])[1]
        assert next_login != login
        assert runtime.service.store.get_user_by_telegram(7001)["user_key"] == user["user_key"]
        assert await runtime.oauth.load_access_token(access_token) is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + access_token},
        ) as http:
            async with streamable_http_client(RESOURCE, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    profile = await session.call_tool("get_profile", {})
                    assert not profile.isError
                    assert profile.structuredContent == {
                        "id": "7001",
                        "name": "Synthetic",
                        "nickname": "Synthetic",
                    }
                    assert json.loads(profile.content[0].text) == profile.structuredContent
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


async def test_button_runtime_keeps_controls_out_of_the_inbox(tmp_path):
    config = Config(
        bot_token="123:synthetic-token",
        auth_token="synthetic-operator-token-for-ui-test-123456",
        database_path=tmp_path / "ui.sqlite3",
        polling_enabled=False,
        allowed_hosts=("connector.example.test",),
        public_mcp_url=RESOURCE,
    )
    bot = SyntheticBot()
    runtime = Runtime(config, bot=bot)
    app = create_http_app(runtime)

    def incoming(text, message_id):
        value = command(text)
        value["message"].update(message_id=message_id, date=1000)
        return value

    def callback(action, query_id):
        return {
            "callback_query": {
                "id": query_id,
                "data": action,
                "from": {"id": 7001, "is_bot": False, "first_name": "Synthetic"},
                "message": {
                    "message_id": 1,
                    "date": 1000,
                    "chat": {"id": 7001, "type": "private"},
                    "from": {"id": 123, "is_bot": True, "username": "synthetic_bot"},
                },
            }
        }

    async with app.router.lifespan_context(app):
        await runtime.handle_update(incoming("/start", 1))
        assert "is_persistent" not in bot.messages[-1]["reply_markup"]
        await runtime.handle_update(incoming("🔌 Подключить AI-приложение", 2))
        assert "Куда подключаем" in bot.messages[-1]["text"]
        assert runtime.service.find_users() == []
        await runtime.handle_update(callback("ui:claude", "synthetic-choose"))
        assert bot.callback_answers == ["synthetic-choose"]
        assert bot.messages[-1]["text"].startswith("Подключение к Claude")
        assert "Код входа:" in bot.messages[-1]["text"]
        assert "is_persistent" not in bot.icons[-1]["reply_markup"]
        assert runtime.inbox.list_messages() == []
        await runtime.handle_update(incoming("Synthetic actual inbox text", 3))
        assert len(runtime.inbox.list_messages()) == 1
        await runtime.handle_update(incoming("⏸ Пауза", 4))
        assert runtime.service.store.get_user_by_telegram(7001)["active"] is True
        await runtime.handle_update(incoming("🔔 Включить", 5))
        assert runtime.service.store.get_user_by_telegram(7001)["active"] is True
        await runtime.handle_update(incoming("🔗 Отключить всё", 6))
        assert runtime.service.store.get_user_by_telegram(7001) is not None
        await runtime.handle_update(callback("ui:unlink_confirm", "synthetic-confirm"))
        assert runtime.service.store.get_user_by_telegram(7001) is None
        assert len(runtime.inbox.list_messages()) == 1
