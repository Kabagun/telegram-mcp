"""Generated PDF bytes pass through the real MCP and downloader contract."""

import sqlite3
from contextlib import closing

import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from telegram_ping_mcp.config import Config
from telegram_ping_mcp.file_inputs import FileDownloader
from telegram_ping_mcp.server import Runtime, create_mcp


async def test_generated_pdf_native_reference_upload_and_renewed_url_retry(tmp_path, monkeypatch):
    await _assert_native_generated_pdf(
        tmp_path, monkeypatch, "files.openai.com", "sig=synthetic-secret"
    )


async def test_generated_azure_pdf_native_reference_upload_and_renewed_url_retry(
    tmp_path, monkeypatch
):
    await _assert_native_generated_pdf(
        tmp_path,
        monkeypatch,
        "oaisdmntprdenmarkeast.blob.core.windows.net",
        "sp=r&sr=b&sv=2025-01-05&se=2030-01-01T00%3A00%3A00Z&sig=synthetic-secret",
    )


async def _assert_native_generated_pdf(tmp_path, monkeypatch, host, query):
    pdf = b"%PDF-1.4\n" + bytes(range(256)) * 448 + b"\n%%EOF"
    requests, uploads = [], []

    class PdfStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield pdf

    async def transport(self, request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == host
        assert request.extensions["sni_hostname"] == host
        assert "authorization" not in request.headers
        return httpx.Response(200, stream=PdfStream(), headers={"content-length": str(len(pdf))})

    async def resolve(requested_host):
        assert requested_host == host
        return ["93.184.216.34"]

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)

    class Bot:
        async def get_me(self):
            return {"username": "synthetic_bot"}

        async def set_commands(self, commands):
            return True

        async def send_document_bytes(self, chat_id, data, **options):
            uploads.append((chat_id, data, options))
            return {
                "message_id": 99,
                "chat": {"id": chat_id, "type": "private"},
                "document": {"file_id": "synthetic-generated-pdf", "file_size": len(data)},
            }

        async def aclose(self):
            pass

    config = Config(database_path=tmp_path / "private.sqlite3", polling_enabled=False)
    runtime = Runtime(config, bot=Bot())
    async with runtime.lifespan():
        runtime.service.store.ensure_private_user(7001, 7001, None, "Synthetic")
        runtime.sender._file_downloader = FileDownloader(resolver=resolve)
        async with create_connected_server_and_client_session(create_mcp(runtime)) as session:
            file = {
                "file_id": "synthetic-generated-file",
                "download_url": f"https://{host}:443/Гайд.pdf?{query}",
                "file_name": "Гайд.pdf",
                "mime_type": "application/pdf",
            }
            arguments = {
                "profile_id": "7001",
                "request_id": "generated-pdf-send",
                "file": file,
            }
            first = await session.call_tool("send_message", arguments)
            assert not first.isError, first.content
            assert first.structuredContent["status"] == "sent"
            file["download_url"] = f"https://{host}/%D0%93%D0%B0%D0%B9%D0%B4.pdf?" + query.replace(
                "synthetic-secret", "renewed"
            )
            second = await session.call_tool("send_message", arguments)
            assert not second.isError and second.structuredContent["duplicate"] is True
        assert len(requests) == len(uploads) == 1
        assert uploads[0][0:2] == (7001, pdf)
        assert uploads[0][2]["file_name"] == "Гайд.pdf"
        assert uploads[0][2]["mime_type"] == "application/pdf"
        with closing(sqlite3.connect(config.database_path)) as database:
            dump = "\n".join(database.iterdump())
            assert "synthetic-secret" not in dump and "renewed" not in dump
            assert database.execute(
                "SELECT text,caption FROM inbound_messages WHERE direction='outgoing'"
            ).fetchall() == [(None, None)]
