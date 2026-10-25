import json
from email.parser import BytesParser
from email.policy import default
from importlib.resources import files

import httpx

from telegram_ping_mcp.service import Service
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramClient


async def test_connect_runs_formatted_message_then_exact_png_through_real_client(tmp_path):
    calls = []

    def api(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, request))
        result = (
            True if method == "setMyCommands" else {"message_id": len(calls), "chat": {"id": 7001}}
        )
        return httpx.Response(200, json={"ok": True, "result": result})

    store = Store(tmp_path / "synthetic.sqlite3")
    invite = store.create_link("synthetic", "Synthetic")
    store.consume_link(invite["token"], 7001, 7001, None, "Synthetic")
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        service = Service(
            store,
            TelegramClient("123:synthetic-token", client=client),
            "synthetic_bot",
            public_mcp_url="https://connector.example.test/mcp",
            login_code_issuer=lambda key: "synthetic-copy-code",
        )
        await service.handle_update(
            {
                "message": {
                    "text": "/connect",
                    "chat": {"id": 7001, "type": "private"},
                    "from": {"id": 7001, "is_bot": False},
                }
            }
        )
    assert [method for method, _ in calls] == ["sendMessage", "sendDocument", "setMyCommands"]
    guide = json.loads(calls[0][1].content)
    assert "Иконка PNG — файлом ниже" in guide["text"]
    assert any(entity["type"] == "bold" for entity in guide["entities"])
    buttons = [b for row in guide["reply_markup"]["inline_keyboard"] for b in row]
    assert any(b.get("copy_text", {}).get("text") == "synthetic-copy-code" for b in buttons)
    assert "parse_mode" not in guide
    request = calls[1][1]
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    parts = {
        part.get_param("name", header="content-disposition"): part for part in message.iter_parts()
    }
    assert parts["chat_id"].get_content() == "7001"
    assert parts["document"].get_filename() == "telegram-icon.png"
    assert parts["document"].get_content_type() == "image/png"
    assert (
        parts["document"].get_payload(decode=True)
        == files("telegram_ping_mcp").joinpath("static", "telegram-icon.png").read_bytes()
    )
