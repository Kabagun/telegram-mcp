"""Return actual attachment content through standard MCP content blocks."""

import base64
import json
from typing import Any
from urllib.parse import quote

from mcp.types import BlobResourceContents, EmbeddedResource, ImageContent, TextContent

AttachmentContent = TextContent | ImageContent | EmbeddedResource


def attachment_content(item: dict[str, Any]) -> list[AttachmentContent]:
    data = item["data"]
    mime = item["mime_type"]
    metadata = {key: value for key, value in item.items() if key != "data"}
    content: list[AttachmentContent] = [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))
    ]
    if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
        else:
            # Do not flood model context when someone uploads a large text file.
            maximum = 60000
            suffix = (
                "\n[Text preview truncated; original bytes follow.]" if len(text) > maximum else ""
            )
            content.append(TextContent(type="text", text=text[:maximum] + suffix))
    encoded = base64.b64encode(data).decode("ascii")
    if mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        content.append(ImageContent(type="image", data=encoded, mimeType=mime))
    else:
        uri = f"telegram-attachment://{item['attachment_id']}/{quote(item['name'], safe='')}"
        content.append(
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=uri,
                    mimeType=mime,
                    blob=encoded,
                ),
            )
        )
    return content
