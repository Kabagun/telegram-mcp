import base64
import copy
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from telegram_ping_mcp.attachments import attachment_content
from telegram_ping_mcp.config import Config
from telegram_ping_mcp.server import Runtime, create_http_app
from telegram_ping_mcp.telegram import TelegramClient


def payload(result):
    assert not result.isError, result.content
    item = result.structuredContent
    if item is None:
        item = json.loads(result.content[0].text)
    return item.get("result", item) if isinstance(item, dict) else item


async def test_real_sdk_receive_file_read_reply_and_deduplicate(tmp_path):
    sent = []

    def api(request):
        if "/file/" in request.url.path:
            return httpx.Response(200, content=b"Synthetic document content")
        method = request.url.path.rsplit("/", 1)[-1]
        arguments = json.loads(request.content)
        if method == "getMe":
            result = {"id": 123, "is_bot": True, "username": "synthetic_bot"}
        elif method == "setMyCommands":
            result = True
        elif method == "getFile":
            result = {"file_id": "synthetic_file", "file_path": "documents/fixture.txt"}
        elif method in {"sendMessage", "sendDocument"}:
            sent.append(arguments)
            result = {"message_id": len(sent), "chat": {"id": arguments["chat_id"]}}
        else:
            raise AssertionError("Unexpected network method")
        return httpx.Response(200, json={"ok": True, "result": result})

    config = Config(
        bot_token="123:synthetic",
        auth_token="synthetic-token-for-http-tests-123456",
        database_path=tmp_path / "inbox.sqlite3",
        polling_enabled=False,
        allowed_group_chat_ids=frozenset({-100123}),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as api_client:
        runtime = Runtime(config, bot=TelegramClient(config.bot_token, client=api_client))
        app = create_http_app(runtime)
        async with app.router.lifespan_context(app):
            store = runtime.service.store
            link = store.create_link("synthetic", "Synthetic")
            store.consume_link(link["token"], 7001, 7001, None, "Synthetic")
            update = {
                "update_id": 20,
                "message": {
                    "message_id": 15,
                    "chat": {"id": 7001, "type": "private"},
                    "from": {"id": 7001, "is_bot": False},
                    "caption": "Please read this",
                    "document": {
                        "file_id": "synthetic_file",
                        "file_name": "notes.txt",
                        "mime_type": "text/plain",
                        "file_size": 26,
                    },
                },
            }
            await runtime.handle_update(update)
            await runtime.handle_update(update)
            assert sent == []  # Incoming ordinary messages never trigger an unsolicited response.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer " + config.auth_token},
            ) as http:
                async with streamable_http_client(
                    "http://127.0.0.1/mcp", http_client=http
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        items = payload(await session.call_tool("list_messages", {}))
                        assert len(items) == 1
                        item = items[0]
                        assert item["user_key"] == "synthetic"
                        assert item["telegram_message_id"] == 15
                        assert "file_id" not in repr(item)
                        content = await session.call_tool(
                            "get_attachment", {"attachment_id": item["attachment"]["id"]}
                        )
                        assert not content.isError
                        assert any(
                            getattr(block, "text", "") == "Synthetic document content"
                            for block in content.content
                        )
                        resource = next(
                            block.resource for block in content.content if block.type == "resource"
                        )
                        assert base64.b64decode(resource.blob) == b"Synthetic document content"
                        path = "/attachments/" + item["attachment"]["id"]
                        assert (
                            await http.get(path, headers={"Authorization": "Bearer wrong"})
                        ).status_code == 401
                        download = await http.get(path)
                        assert download.status_code == 200
                        assert download.content == b"Synthetic document content"
                        assert download.headers["cache-control"] == "no-store"
                        assert download.headers["content-disposition"].startswith("attachment;")
                        assert download.headers["x-accel-buffering"] == "no"
                        assert (
                            await http.get(path, headers={"Host": "attacker.invalid"})
                        ).status_code == 421
                        args = {
                            "message_id": item["id"],
                            "text": "Read your file",
                            "request_id": "reply-1",
                        }
                        result = payload(await session.call_tool("reply_message", args))
                        assert result["status"] == "sent"
                        assert sent[-1]["reply_parameters"] == {
                            "message_id": 15,
                            "allow_sending_without_reply": False,
                        }
                        assert payload(await session.call_tool("reply_message", args))["duplicate"]
                        assert len(sent) == 1
                        assert payload(
                            await session.call_tool("mark_read", {"message_id": item["id"]})
                        )["read"]
                        group = copy.deepcopy(update)
                        group["message"]["chat"] = {"id": -100123, "type": "supergroup"}
                        group["message"]["message_thread_id"] = 17
                        group["message"]["message_id"] = 16
                        await runtime.handle_update(group)
                        grouped = payload(
                            await session.call_tool("list_messages", {"after_id": item["id"]})
                        )[0]
                        assert (
                            payload(
                                await session.call_tool(
                                    "reply_message",
                                    {
                                        "message_id": grouped["id"],
                                        "text": "Thread reply",
                                        "request_id": "reply-2",
                                    },
                                )
                            )["status"]
                            == "sent"
                        )
                        assert sent[-1]["chat_id"] == -100123
                        assert sent[-1]["message_thread_id"] == 17
                        file_args = {
                            "message_id": item["id"],
                            "caption": "Original file received",
                            "request_id": "file-reply-1",
                        }
                        file_result = payload(
                            await session.call_tool("reply_attachment", file_args)
                        )
                        assert file_result["status"] == "sent"
                        assert sent[-1]["document"] == "synthetic_file"
                        assert sent[-1]["chat_id"] == 7001
                        assert sent[-1]["reply_parameters"]["message_id"] == 15
                        assert payload(await session.call_tool("reply_attachment", file_args))[
                            "duplicate"
                        ]
                        store.set_active_by_telegram(7001, False)
                        assert (
                            await session.call_tool(
                                "get_attachment", {"attachment_id": item["attachment"]["id"]}
                            )
                        ).isError
                        assert (
                            await session.call_tool(
                                "reply_message", {**args, "request_id": "reply-disabled"}
                            )
                        ).isError
                        assert (
                            await session.call_tool(
                                "reply_attachment", {**file_args, "request_id": "file-disabled"}
                            )
                        ).isError
                        assert len(sent) == 3


def test_images_and_binary_content_retain_bytes():
    item = {
        "data": b"synthetic",
        "mime_type": "image/png",
        "name": "image.png",
        "attachment_id": "a" * 32,
    }
    blocks = attachment_content(item)
    assert blocks[-1].type == "image"
    assert base64.b64decode(blocks[-1].data) == item["data"]
    item.update(mime_type="application/octet-stream", name="report #1.bin")
    blocks = attachment_content(item)
    assert blocks[-1].type == "resource"
    assert "%23" in str(blocks[-1].resource.uri)
    assert base64.b64decode(blocks[-1].resource.blob) == item["data"]
