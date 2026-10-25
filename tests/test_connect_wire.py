import hashlib
import json
import sqlite3
from email.parser import BytesParser
from email.policy import default
from importlib.resources import files

import httpx

from telegram_ping_mcp.oauth import OAuthProvider
from telegram_ping_mcp.onboarding import menu_keyboard, onboarding_text, unlink_keyboard
from telegram_ping_mcp.service import Service
from telegram_ping_mcp.store import Store
from telegram_ping_mcp.telegram import TelegramClient


async def test_contextual_help_and_back_use_same_message_through_real_client_without_code_rotation(
    tmp_path,
):
    calls = []
    code = "SYNTHETIC-🙂-<&_`>"
    endpoint = "https://connector.example.test/🙂/mcp"
    original = onboarding_text(endpoint, login_code=code)

    def api(request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        calls.append((method, payload))
        result = (
            True
            if method == "answerCallbackQuery"
            else {"message_id": payload["message_id"], "chat": {"id": 7001}}
        )
        return httpx.Response(200, json={"ok": True, "result": result})

    def query(action, text):
        return {
            "callback_query": {
                "id": "synthetic-callback",
                "data": action,
                "from": {"id": 7001, "is_bot": False},
                "message": {
                    "message_id": 23,
                    "date": 1,
                    "text": text,
                    "chat": {"id": 7001, "type": "private"},
                    "from": {"id": 9999, "is_bot": True, "username": "synthetic_bot"},
                },
            }
        }

    def forbidden(user_key):
        raise AssertionError("Contextual details cannot issue another code")

    store = Store(tmp_path / "contextual-wire.sqlite3")
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        service = Service(
            store,
            TelegramClient("123:synthetic-token", client=client),
            "synthetic_bot",
            public_mcp_url=endpoint,
            login_code_issuer=forbidden,
        )
        await service.handle_update(query("ui:help_chatgpt", original))
        expanded = calls[-1][1]
        assert expanded["text"].startswith(original)
        await service.handle_update(query("ui:details_back", expanded["text"]))

    assert [method for method, _ in calls] == [
        "answerCallbackQuery",
        "editMessageText",
        "answerCallbackQuery",
        "editMessageText",
    ]
    for method, payload in calls:
        if method != "editMessageText":
            continue
        assert payload["chat_id"] == 7001
        assert payload["message_id"] == 23
        assert "parse_mode" not in payload
        buttons = [button for row in payload["reply_markup"]["inline_keyboard"] for button in row]
        assert any(button.get("copy_text", {}).get("text") == code for button in buttons)
    assert calls[-1][1]["text"] == original
    assert store.list_users() == []


async def test_unlink_reply_keyboard_removes_one_bound_form_and_restores_main_navigation(tmp_path):
    calls = []

    def api(request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        calls.append((method, payload))
        result = (
            True
            if method in {"setMyCommands", "deleteMessage"}
            else {"message_id": len(calls), "chat": {"id": 7001}}
        )
        return httpx.Response(200, json={"ok": True, "result": result})

    def message(text):
        return {
            "message": {
                "text": text,
                "chat": {"id": 7001, "type": "private"},
                "from": {"id": 7001, "is_bot": False, "first_name": "Synthetic"},
            }
        }

    store = Store(tmp_path / "unlink-wire.sqlite3")
    invite = store.create_link("synthetic", "Synthetic")
    store.consume_link(invite["token"], 7001, 7001, None, "Synthetic")
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        service = Service(
            store, TelegramClient("123:synthetic-token", client=client), "synthetic_bot"
        )
        await service.handle_update(message("🔗 Отключить всё"))
        await service.handle_update(message("🔗 Отключить всё"))
        assert len(calls) == 1
        assert calls[0][1]["reply_markup"] == unlink_keyboard()
        assert "parse_mode" not in calls[0][1]

        await service.handle_update(message("Отмена"))
        assert calls[1] == ("deleteMessage", {"chat_id": 7001, "message_id": 1})
        assert calls[2][1]["reply_markup"] == menu_keyboard(linked=True)
        assert store.get_user("synthetic") is not None

        await service.handle_update(message("🔗 Отключить всё"))
        await service.handle_update(message("Отключить всё"))
        assert calls[5] == ("deleteMessage", {"chat_id": 7001, "message_id": 5})
        assert calls[6][1]["reply_markup"] == menu_keyboard()
        assert store.get_user("synthetic") is None
        assert calls[-1][1]["commands"] == []


async def test_client_callback_acknowledges_then_sends_guide_and_exact_png_through_real_client(
    tmp_path,
):
    calls = []
    code = "synthetic-🙂-copy-code"

    def api(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, request))
        result = (
            True
            if method in {"setMyCommands", "answerCallbackQuery"}
            else {"message_id": len(calls), "chat": {"id": 7001}}
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
            login_code_issuer=lambda key: code,
        )
        await service.handle_update(
            {
                "callback_query": {
                    "id": "synthetic-callback",
                    "data": "ui:chatgpt",
                    "from": {"id": 7001, "is_bot": False},
                    "message": {
                        "message_id": 11,
                        "date": 1,
                        "chat": {"id": 7001, "type": "private"},
                        "from": {"id": 9999, "is_bot": True, "username": "synthetic_bot"},
                    },
                }
            }
        )
    assert [method for method, _ in calls] == [
        "answerCallbackQuery",
        "sendMessage",
        "sendDocument",
        "setMyCommands",
    ]
    assert json.loads(calls[0][1].content) == {"callback_query_id": "synthetic-callback"}
    guide = json.loads(calls[1][1].content)
    assert "Иконка PNG — файлом ниже" in guide["text"]
    sections = (
        "Название: Telegram\n",
        "URL-адрес сервера: https://connector.example.test/mcp\n",
        "Аутентификация: OAuth\n\n🔑 Код подключения\n",
        f"Код входа: {code}\n",
        "Вставьте код в окне подключения.\n",
        "📎 Иконка PNG — файлом ниже.",
    )
    offsets = [guide["text"].index(section) for section in sections]
    assert offsets == sorted(offsets)
    assert "Без срока действия · одноразовый." in guide["text"]
    assert "10 минут" not in guide["text"]
    formatted = {}
    encoded = guide["text"].encode("utf-16-le")
    for entity in guide["entities"]:
        start = entity["offset"] * 2
        end = start + entity["length"] * 2
        formatted[encoded[start:end].decode("utf-16-le")] = entity
    assert formatted[code]["type"] == "code"
    assert (
        formatted[code]["offset"]
        == len(guide["text"][: guide["text"].index(code)].encode("utf-16-le")) // 2
    )
    assert formatted[code]["length"] == len(code.encode("utf-16-le")) // 2
    for heading in ("Подключение к ChatGPT", "Настройки плагина", "🔑 Код подключения"):
        assert formatted[heading]["type"] == "bold"
    buttons = [b for row in guide["reply_markup"]["inline_keyboard"] for b in row]
    assert any(b.get("copy_text", {}).get("text") == code for b in buttons)
    assert "parse_mode" not in guide
    request = calls[2][1]
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    parts = {
        part.get_param("name", header="content-disposition"): part for part in message.iter_parts()
    }
    assert parts["chat_id"].get_content() == "7001"
    assert json.loads(
        parts["reply_markup"].get_payload(decode=True).decode("utf-8")
    ) == menu_keyboard(linked=True)
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
        assert guide["text"].index("Аутентификация: OAuth\n") < guide["text"].index(code)
        assert guide["text"].index(code) < guide["text"].index("Вставьте код в окне подключения.\n")
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
