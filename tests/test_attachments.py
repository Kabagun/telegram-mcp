import base64
import json

from telegram_ping_mcp.attachments import INLINE_ATTACHMENT_BYTES, attachment_content


def item(data=b"synthetic content", mime="application/octet-stream"):
    return {
        "attachment_id": "synthetic-handle",
        "message_id": 1,
        "data": data,
        "name": "synthetic.bin",
        "mime_type": mime,
        "size": len(data),
    }


def test_small_binary_uses_standard_blob_resource():
    source = item()
    content = attachment_content(source)
    assert json.loads(content[0].text)["size"] == len(source["data"])
    assert content[1].type == "resource"
    assert base64.b64decode(content[1].resource.blob) == source["data"]


def test_small_image_uses_standard_image_content():
    source = item(b"synthetic image", "image/png")
    content = attachment_content(source)
    assert content[1].type == "image"
    assert base64.b64decode(content[1].data) == source["data"]


def test_metadata_only_attachment_has_authenticated_download_instructions():
    source = item()
    del source["data"]
    source["size"] = 100 * 1024 * 1024
    content = attachment_content(source)
    assert len(content) == 1
    metadata = json.loads(content[0].text)
    assert metadata["download_path"] == "/attachments/synthetic-handle"
    assert "Bearer authorization" in metadata["download_instructions"]
    assert "data" not in metadata


def test_large_file_is_never_base64_encoded_or_previewed(monkeypatch):
    def forbidden_encoding(*_):
        raise AssertionError("Large files must not be base64 encoded.")

    monkeypatch.setattr(base64, "b64encode", forbidden_encoding)
    content = attachment_content(item(b"x" * (INLINE_ATTACHMENT_BYTES + 1), "text/plain"))
    assert len(content) == 1
    assert len(content[0].text) < 1000
    assert json.loads(content[0].text)["size"] == INLINE_ATTACHMENT_BYTES + 1
