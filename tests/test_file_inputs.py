from __future__ import annotations

import ipaddress

import httpx
import pytest

from telegram_ping_mcp.file_inputs import FileDownloader, _PinnedTransport, parse_file_input
from telegram_ping_mcp.service import ServiceError

FILE = {
    "download_url": "https://files.oaiusercontent.com/synthetic/file?signature=synthetic",
    "file_id": "file-synthetic",
    "file_name": "../example.txt",
    "mime_type": "text/plain",
}
GENERATED_PDF = {
    "download_url": "https://files.openai.com/content/synthetic-generated/report.pdf?signature=synthetic",
    "file_id": "file-synthetic-generated-pdf",
    "file_name": "Синтетический отчёт.pdf",
    "mime_type": "application/pdf",
}


def test_file_adapter_preserves_safe_metadata_and_hides_signed_url_in_repr():
    parsed = parse_file_input(FILE)
    assert parsed.file_name == "example.txt"
    assert parsed.mime_type == "text/plain"
    assert "signature" not in repr(parsed) and "file-synthetic" not in repr(parsed)
    assert (
        parsed.fingerprint_data()
        == parse_file_input(
            {**FILE, "download_url": FILE["download_url"] + "-renewed"}
        ).fingerprint_data()
    )


@pytest.mark.parametrize("name", ["😀" * 100, "é" * 200, "../" + "界" * 100])
def test_file_adapter_filename_is_a_safe_basename_with_at_most_255_utf8_bytes(name):
    parsed = parse_file_input({**FILE, "file_name": name})
    assert 1 <= len(parsed.file_name.encode("utf-8")) <= 255
    assert "/" not in parsed.file_name and "\\" not in parsed.file_name
    assert "�" not in parsed.file_name


@pytest.mark.parametrize(
    "mime", ["!application/plain", "application/!plain", "text/plain; charset=utf-8"]
)
def test_mime_normalization_matches_upload_transport_grammar(mime):
    assert parse_file_input({**FILE, "mime_type": mime}).mime_type == "application/octet-stream"


@pytest.mark.parametrize(
    "url",
    [
        "http://files.oaiusercontent.com/file",
        "https://localhost/file",
        "https://127.0.0.1/file",
        "https://[::1]/file",
        "https://oaiusercontent.com/file",
        "https://eviloaiusercontent.com/file",
        "https://files.oaiusercontent.com.evil.example/file",
        "https://files.oaiusercontent.com:444/file",
        "https://user:secret@files.oaiusercontent.com/file",
        "https://files.oaiusercontent.com/file#fragment",
        "https://files.oaiusercontent.com/file#",
        "https://files.oaiusercontent.com/file\n",
        "https://files.oaiusercontent.com/file%0d%0a",
        "https://files.oaiusercontent.com\\@example.com/file",
        "file:///tmp/example.txt",
        "D:/example.txt",
        "https://files.oaiusercontent.com./file",
        "https://-bad.oaiusercontent.com/file",
        "https://files.oaiusercontent.com/file%5cname",
    ],
)
def test_unsafe_or_non_adapter_sources_are_rejected_without_url_in_error(url):
    with pytest.raises(ServiceError) as caught:
        parse_file_input({**FILE, "download_url": url})
    assert url not in str(caught.value)


@pytest.mark.parametrize(
    "file",
    [
        "https://files.oaiusercontent.com/file",
        {},
        {"download_url": FILE["download_url"]},
        {**FILE, "path": "example.txt"},
        {**FILE, "file_id": ""},
        {**FILE, "file_id": "x\x00"},
        {**FILE, "file_name": None},
        {**FILE, "mime_type": None},
        {**FILE, "file_name": "x" * 1025},
    ],
)
def test_adapter_requires_exact_client_contract(file):
    with pytest.raises(ServiceError):
        parse_file_input(file)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk


def downloader_for(response, *, addresses=("93.184.216.34",), host="files.oaiusercontent.com"):
    calls = []
    options = []

    async def handler(request):
        calls.append(request)
        return response

    def factory(**kwargs):
        options.append(kwargs)
        return httpx.AsyncClient(**{**kwargs, "transport": httpx.MockTransport(handler)})

    async def resolver(requested_host):
        assert requested_host == host
        return addresses

    return FileDownloader(client_factory=factory, resolver=resolver), calls, options


async def test_file_download_is_bounded_streaming_and_disables_proxy_and_redirects():
    response = httpx.Response(
        200, headers={"content-length": "9"}, stream=Chunks([b"synt", b"hetic"])
    )
    downloader, calls, options = downloader_for(response)
    assert await downloader.download(parse_file_input(FILE), max_bytes=9) == b"synthetic"
    assert len(calls) == 1 and calls[0].headers["accept-encoding"] == "identity"
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert isinstance(options[0]["transport"], _PinnedTransport)
    assert options[0]["transport"]._address == "93.184.216.34"


@pytest.mark.parametrize(
    "addresses",
    [
        (),
        ("127.0.0.1",),
        ("10.1.1.1",),
        ("169.254.169.254",),
        ("::1",),
        ("fd00::1",),
        ("::ffff:127.0.0.1",),
        ("93.184.216.34", "192.168.1.1"),
        ("invalid",),
    ],
)
async def test_private_mixed_or_invalid_dns_results_never_reach_http(addresses):
    assert not addresses or any(
        address == "invalid" or not ipaddress.ip_address(address).is_global for address in addresses
    )
    downloader, calls, _ = downloader_for(httpx.Response(200), addresses=addresses)
    with pytest.raises(ServiceError, match="could not be downloaded"):
        await downloader.download(parse_file_input(FILE), max_bytes=9)
    assert calls == []


@pytest.mark.parametrize(
    "status,headers,chunks",
    [
        (302, {"location": "http://127.0.0.1/private"}, [b"ignored"]),
        (500, {}, [b"synthetic secret body"]),
        (200, {"content-length": "10"}, [b"1234567890"]),
        (200, {"content-length": "invalid"}, [b"ignored"]),
        (200, {"content-length": "2"}, [b"123"]),
        (200, {"content-encoding": "gzip"}, [b"ignored"]),
        (200, {"synthetic": "x" * (16 * 1024)}, [b"ignored"]),
    ],
)
async def test_response_rejection_never_exposes_body_or_redirects(status, headers, chunks):
    stream = Chunks(chunks)
    downloader, calls, _ = downloader_for(httpx.Response(status, headers=headers, stream=stream))
    with pytest.raises(ServiceError) as caught:
        await downloader.download(parse_file_input(FILE), max_bytes=9)
    assert "secret" not in str(caught.value)
    assert "127.0.0.1" not in str(caught.value)
    assert len(calls) == 1
    if status != 200 or headers.get("content-length") == "10":
        assert stream.read == 0


async def test_download_without_content_length_stops_at_memory_limit():
    # Each raw chunk remains bounded even when a source never supplies a length.
    stream = Chunks([b"a" * 65536, b"b" * 65536, b"c" * 65536])
    downloader, _, _ = downloader_for(httpx.Response(200, stream=stream))
    with pytest.raises(ServiceError, match="exceeds"):
        await downloader.download(parse_file_input(FILE), max_bytes=65536)
    assert stream.read == 2


async def test_pinned_transport_keeps_original_tls_name_and_host(monkeypatch):
    observed = []

    async def send(self, request):
        observed.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    async with _PinnedTransport("files.oaiusercontent.com", "93.184.216.34") as transport:
        await transport.handle_async_request(httpx.Request("GET", FILE["download_url"]))
    request = observed[0]
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "files.oaiusercontent.com"
    assert request.extensions["sni_hostname"] == "files.oaiusercontent.com"


async def test_network_exception_is_content_free():
    async def resolver(host):
        raise RuntimeError("synthetic signed URL secret")

    with pytest.raises(ServiceError) as caught:
        await FileDownloader(resolver=resolver).download(parse_file_input(FILE), max_bytes=9)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("host", ["files.openai.com", "files.oaiusercontent.com"])
@pytest.mark.parametrize("port", ["", ":443"])
def test_native_attached_and_generated_sources_accept_the_default_https_port(host, port):
    parsed = parse_file_input(
        {
            **GENERATED_PDF,
            "download_url": f"https://{host}{port}/content/report.pdf?signature=synthetic",
        }
    )
    assert parsed.mime_type == "application/pdf"
    assert parsed.file_name == GENERATED_PDF["file_name"]
    assert parsed.source_identity == f"https://{host}/content/report.pdf"
    assert "signature" not in repr(parsed)


def test_generated_pdf_url_unicode_path_and_query_have_a_canonical_retry_identity():
    url = (
        "https://files.openai.com:443/content/Синтетический отчёт.pdf?filename=отчёт.pdf&sig=first"
    )
    parsed = parse_file_input({**GENERATED_PDF, "download_url": url})
    canonical = httpx.URL(url).copy_with(query=None)
    assert parsed.source_identity == str(canonical)
    assert parsed.download_url == url
    assert "sig=" not in parsed.source_identity
    assert ":443" not in parsed.source_identity
    assert (
        parsed.fingerprint_data()
        == parse_file_input(
            {**GENERATED_PDF, "download_url": str(canonical) + "?sig=renewed&filename=другой.pdf"}
        ).fingerprint_data()
    )
    assert (
        parse_file_input(
            {
                **GENERATED_PDF,
                "download_url": "https://files.openai.com/content/different.pdf?sig=first",
            }
        ).fingerprint_data()
        != parsed.fingerprint_data()
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://openai.com/content/file.pdf",
        "https://chat.openai.com/content/file.pdf",
        "https://sub.files.openai.com/content/file.pdf",
        "https://evilfiles.openai.com/content/file.pdf",
        "https://files.openai.com.evil.example/content/file.pdf",
        "https://files.openai.com:80/content/file.pdf",
        "https://files.openai.com:8443/content/file.pdf",
        "https://files.openai.com:/content/file.pdf",
        "https://files.openai.com:0443/content/file.pdf",
        "https://user:synthetic-secret@files.openai.com/content/file.pdf",
        "https://files.openai.com@evil.example/content/file.pdf",
        "https://fіles.openai.com/content/file.pdf",  # Cyrillic i.
        "https://files.openаi.com/content/file.pdf",  # Cyrillic a.
        "https://files.openai.com./content/file.pdf",
        " https://files.openai.com/content/file.pdf",
        "https://files.openai.com/content/отчёт%0a.pdf",
        "https://files.openai.com/content/отчёт.pdf?name=%00",
        "https://files.openai.com/content/отчёт.pdf?name=%5cprivate",
        "https://files.openai.com/content/отчёт\u202e.pdf",
        "https://files.openai.com/content/отчёт\ud800.pdf",
        "https://files.openai.com/content/file.pdf#fragment",
        "https://files.openai.com/content/file.pdf\\name",
        "https://files.openai.com\t/content/file.pdf",
    ],
)
def test_generated_file_source_keeps_exact_host_and_safe_url_boundaries(url):
    with pytest.raises(ServiceError) as caught:
        parse_file_input({**GENERATED_PDF, "download_url": url})
    assert str(caught.value) == (
        "File input is invalid; use an attached or generated file supplied by the native client."
    )
    assert url not in str(caught.value)


@pytest.mark.parametrize(
    "addresses",
    [("127.0.0.1",), ("169.254.169.254",), ("::1",), ("93.184.216.34", "10.0.0.1")],
)
async def test_generated_file_host_still_requires_only_public_dns_results(addresses):
    downloader, calls, options = downloader_for(
        httpx.Response(200), addresses=addresses, host="files.openai.com"
    )
    with pytest.raises(ServiceError, match="could not be downloaded"):
        await downloader.download(parse_file_input(GENERATED_PDF), max_bytes=200000)
    assert calls == []
    assert options == []


async def test_generated_pdf_download_preserves_bytes_and_uses_pinned_host_and_tls(monkeypatch):
    data = (b"%PDF-1.7\n" + b"synthetic-pdf-body\n" * 6400)[: 112 * 1024]
    assert len(data) == 112 * 1024
    calls = []
    resolved = []
    url = (
        "https://files.openai.com:443/content/Синтетический отчёт.pdf"
        "?signature=synthetic&name=отчёт"
    )

    async def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-length": str(len(data)), "content-type": "application/pdf"},
            stream=Chunks([data[:65536], data[65536:]]),
        )

    mock_transport = httpx.MockTransport(handler)

    async def pinned_send(self, request):
        # Run the real _PinnedTransport rewrite and stop at HTTPX's network boundary.
        return await mock_transport.handle_async_request(request)

    async def resolver(host):
        resolved.append(host)
        return ["93.184.216.34"]

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", pinned_send)
    parsed = parse_file_input({**GENERATED_PDF, "download_url": url})
    result = await FileDownloader(resolver=resolver).download(parsed, max_bytes=200000)
    assert result == data
    assert resolved == ["files.openai.com"]
    assert len(calls) == 1
    request = calls[0]
    assert request.url.host == "93.184.216.34"
    assert request.url.port is None
    assert request.headers["host"] == "files.openai.com"
    assert request.extensions["sni_hostname"] == "files.openai.com"
    assert request.headers["accept-encoding"] == "identity"
    expected = httpx.URL(url)
    assert request.url.raw_path == expected.raw_path


async def test_generated_file_redirect_does_not_follow_or_read_response_body():
    stream = Chunks([b"synthetic private redirect body"])
    downloader, calls, options = downloader_for(
        httpx.Response(302, headers={"location": "http://127.0.0.1/private"}, stream=stream),
        host="files.openai.com",
    )
    with pytest.raises(ServiceError, match="could not be downloaded"):
        await downloader.download(parse_file_input(GENERATED_PDF), max_bytes=200000)
    assert len(calls) == 1
    assert options[0]["follow_redirects"] is False
    assert stream.read == 0
