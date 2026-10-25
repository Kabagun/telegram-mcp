import hashlib
import json
import sqlite3
from email.parser import BytesParser
from email.policy import default
from importlib.resources import files

import httpx

from telegram_ping_mcp.oauth import OAuthProvider
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


async def test_new_private_connect_issues_own_oauth_code_without_invitation_and_replaces_it(
    tmp_path,
):
    calls = []

    def api(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, request))
        result = (
            True if method == "setMyCommands" else {"message_id": len(calls), "chat": {"id": 7001}}
        )
        return httpx.Response(200, json={"ok": True, "result": result})

    store = Store(tmp_path / "new-private.sqlite3")
    oauth = OAuthProvider(
        tmp_path / "oauth.sqlite3",
        issuer_url="https://connector.example.test",
        resource_url="https://connector.example.test/mcp",
        get_user=store.get_user,
    )
    update = {
        "message": {
            "text": "/connect@SYNTHETIC_BOT",
            "chat": {"id": 7001, "type": "private"},
            "from": {
                "id": 7001,
                "is_bot": False,
                "username": "synthetic_user",
                "first_name": "Synthetic 🙂",
            },
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        service = Service(
            store,
            TelegramClient("123:synthetic-token", client=client),
            "synthetic_bot",
            public_mcp_url="https://connector.example.test/mcp",
            login_code_issuer=oauth.issue_login_code,
        )
        assert store.list_users() == []

        await service.handle_update(update)

        user = store.get_user_by_telegram(7001)
        assert user is not None
        assert user["user_key"].startswith("tg_")
        assert user["telegram_user_id"] == user["chat_id"] == 7001
        assert user["username"] == "synthetic_user"
        assert user["first_name"] == "Synthetic 🙂"
        assert user["active"] is True

        await service.handle_update(update)

    assert [method for method, _ in calls] == [
        "sendMessage",
        "sendDocument",
        "setMyCommands",
        "sendMessage",
        "sendDocument",
        "setMyCommands",
    ]
    codes = []
    for method, request in calls:
        if method != "sendMessage":
            continue
        guide = json.loads(request.content)
        assert guide["chat_id"] == 7001
        assert "приглашение" not in guide["text"]
        assert "URL-адрес сервера: https://connector.example.test/mcp\n" in guide["text"]
        code = guide["text"].split("Код входа: ", 1)[1].split("\n", 1)[0]
        codes.append(code)
        buttons = [button for row in guide["reply_markup"]["inline_keyboard"] for button in row]
        assert any(button.get("copy_text", {}).get("text") == code for button in buttons)
    assert len(codes) == 2 and codes[0] != codes[1]
    assert store.list_users() == [user]
    with sqlite3.connect(store.path) as database:
        assert database.execute("SELECT COUNT(*) FROM link_tokens").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
        store_dump = "\n".join(database.iterdump())
    with sqlite3.connect(oauth.database_path) as database:
        rows = database.execute(
            "SELECT user_key, telegram_user_id, code_hash FROM login_codes"
        ).fetchall()
        oauth_dump = "\n".join(database.iterdump())
    assert rows == [
        (user["user_key"], 7001, hashlib.sha256(codes[1].replace("-", "").encode()).hexdigest())
    ]
    for code in codes:
        assert code not in store_dump + oauth_dump
        assert code.replace("-", "") not in store_dump + oauth_dump
