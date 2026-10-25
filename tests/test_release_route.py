from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from telegram_ping_mcp.config import Config
from telegram_ping_mcp.onboarding import menu_keyboard
from telegram_ping_mcp.server import Runtime, create_http_app

BASE = "https://connector.example.test"
MASTER = "synthetic-operator-token-for-release-tests-123456"


class Bot:
    def __init__(self):
        self.sent = []

    async def get_me(self):
        return {"username": "synthetic_bot"}

    async def set_commands(self, commands, **kwargs):
        return None

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return {"message_id": len(self.sent), "chat": {"id": chat_id}}

    async def aclose(self):
        return None


@asynccontextmanager
async def client_for(tmp_path, monkeypatch):
    monkeypatch.setattr("telegram_ping_mcp.server.installed_version", lambda _: "1.2.3")
    bot = Bot()
    runtime = Runtime(
        Config(
            bot_token="123:synthetic-token",
            auth_token=MASTER,
            polling_enabled=False,
            database_path=tmp_path / "state.sqlite3",
            allowed_hosts=("connector.example.test",),
            public_mcp_url=BASE + "/mcp",
        ),
        bot=bot,
    )

    async def load_token(token):
        if token != "synthetic-oauth":
            return None
        return SimpleNamespace(
            resource=BASE + "/mcp", scopes=["telegram"], user_key="synthetic", telegram_user_id=7001
        )

    monkeypatch.setattr(runtime.oauth, "load_access_token", load_token)
    app = create_http_app(runtime)
    async with app.router.lifespan_context(app):
        store = runtime.require_service().store
        invitation = store.create_link("synthetic", "Synthetic")
        store.consume_link(invitation["token"], 7001, 7001, None, "Synthetic")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as client:
            yield client, bot


async def test_release_route_requires_operator_and_runs_only_after_explicit_post(
    tmp_path, monkeypatch
):
    async with client_for(tmp_path, monkeypatch) as (client, bot):
        assert bot.sent == []
        response = await client.post("/admin/releases/1.2.3/broadcast", json={"message": "Release"})
        assert response.status_code == 401
        response = await client.post(
            "/admin/releases/1.2.3/broadcast",
            json={"message": "Release"},
            headers={"Authorization": "Bearer synthetic-oauth"},
        )
        assert response.status_code == 403
        assert bot.sent == []
        headers = {"Authorization": "Bearer " + MASTER}
        response = await client.post(
            "/admin/releases/1.2.3/broadcast", json={"message": "Release"}, headers=headers
        )
        assert response.status_code == 200
        assert response.json() == {"sent": 1, "failed": 0}
        assert bot.sent == [
            (
                7001,
                "Release",
                {"disable_notification": True, "reply_markup": menu_keyboard(linked=True)},
            )
        ]
        response = await client.post(
            "/admin/releases/1.2.3/broadcast", json={"message": "Release"}, headers=headers
        )
        assert response.json() == {"sent": 0, "failed": 0}
        assert len(bot.sent) == 1


@pytest.mark.parametrize(
    "version,payload,extra_headers,status",
    [
        ("1.0.0", {"message": "Release"}, {}, 400),
        ("1.2.3", {"message": "Release", "extra": True}, {}, 400),
        ("1.2.3", {"message": "Release", "retry_failed": 1}, {}, 400),
        ("1.2.3", {"message": "Release"}, {"Origin": "https://foreign.example.test"}, 403),
        ("1.2.3", {"message": "Release"}, {"Content-Type": "text/plain"}, 415),
        ("1.2.3", {"message": "x" * 32769}, {}, 413),
    ],
)
async def test_invalid_release_request_cannot_send(
    tmp_path, monkeypatch, version, payload, extra_headers, status
):
    async with client_for(tmp_path, monkeypatch) as (client, bot):
        response = await client.post(
            f"/admin/releases/{version}/broadcast",
            json=payload,
            headers={"Authorization": "Bearer " + MASTER, **extra_headers},
        )
        assert response.status_code == status
        assert bot.sent == []
