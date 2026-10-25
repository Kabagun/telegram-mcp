import struct
from dataclasses import replace

import httpx
import pytest

from telegram_ping_mcp.config import Config
from telegram_ping_mcp.server import Runtime, create_http_app


class SilentBot:
    async def get_me(self):
        return {"username": "synthetic_bot"}

    async def set_commands(self, commands, *, chat_id=None):
        pass

    async def aclose(self):
        pass


@pytest.mark.parametrize("oauth_enabled", [False, True])
async def test_icon_download_fits_plugin_form_without_opening_private_routes(
    tmp_path, oauth_enabled
):
    config = Config(
        bot_token="123456:synthetic-token",
        auth_token="synthetic-operator-token-with-enough-characters",
        polling_enabled=False,
        database_path=tmp_path / "synthetic.sqlite3",
    )
    if oauth_enabled:
        config = replace(config, public_mcp_url="https://127.0.0.1/mcp")
    app = create_http_app(Runtime(config, bot=SilentBot()))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://127.0.0.1"
        ) as client:
            response = await client.get("/assets/telegram-icon.png")
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            assert 'filename="telegram-icon.png"' in response.headers["content-disposition"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
            assert len(response.content) <= 10 * 1024
            assert struct.unpack(">II", response.content[16:24]) == (256, 256)
            assert (await client.head("/assets/telegram-icon.png")).content == b""
            assert (
                await client.get("/assets/telegram-icon.png", headers={"Host": "attacker.invalid"})
            ).status_code == 421
            assert (await client.post("/mcp")).status_code == 401
            assert (await client.get("/attachments/unknown")).status_code == 401
            assert (await client.get("/assets/server.env")).status_code == 401
