"""Per-request OAuth ownership checks for MCP tools and attachment downloads."""

from __future__ import annotations

from typing import Any, TypedDict

from mcp.server.lowlevel.server import request_ctx
from starlette.requests import Request

_PRINCIPAL_SCOPE_KEY = "telegram_oauth_user"
_DENIED = "Access denied."


class OAuthPrincipal(TypedDict):
    user_key: str
    telegram_user_id: int


def principal(*, request: Request | None = None) -> OAuthPrincipal | None:
    """Read the authenticated identity from the current HTTP request.

    The SDK carries the original Request in message metadata when it starts
    handler tasks. A ContextVar set by HTTP middleware alone is insufficient
    because stateless server tasks belong to the session manager's task group.
    Memory/stdio calls and HTTP requests authenticated by the legacy master
    bearer have no OAuth marker and retain their existing access.
    """
    if request is None:
        context = request_ctx.get(None)
        request = context.request if context is not None else None
    if request is None or _PRINCIPAL_SCOPE_KEY not in request.scope:
        return None

    identity = request.scope[_PRINCIPAL_SCOPE_KEY]
    if not isinstance(identity, dict):
        raise ValueError(_DENIED)
    user_key = identity.get("user_key")
    telegram_user_id = identity.get("telegram_user_id")
    if (
        not isinstance(user_key, str)
        or not user_key
        or isinstance(telegram_user_id, bool)
        or not isinstance(telegram_user_id, int)
        or not 1 <= telegram_user_id <= 2**63 - 1
    ):
        raise ValueError(_DENIED)
    return {"user_key": user_key, "telegram_user_id": telegram_user_id}


def is_oauth(*, request: Request | None = None) -> bool:
    """Return whether this request uses a valid per-user OAuth identity."""
    return principal(request=request) is not None


def require_user(user_key: str, *, request: Request | None = None) -> None:
    """Allow an OAuth caller to select only their own verified user record."""
    identity = principal(request=request)
    if identity is not None and user_key != identity["user_key"]:
        raise ValueError(_DENIED)


def require_private_chat(chat_id: int | None, *, request: Request | None = None) -> int | None:
    """Constrain OAuth inbox queries to the caller's private Telegram chat."""
    identity = principal(request=request)
    if identity is None:
        return chat_id
    if chat_id is None:
        return identity["telegram_user_id"]
    if (
        isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or chat_id != identity["telegram_user_id"]
    ):
        raise ValueError(_DENIED)
    return chat_id


def require_message(message: dict[str, Any] | None, *, request: Request | None = None) -> None:
    """Authorize an existing inbox item without exposing why access failed."""
    identity = principal(request=request)
    if message is None:
        raise ValueError(_DENIED)
    if identity is None:
        return
    chat_id = message.get("chat_id")
    if (
        message.get("user_key") != identity["user_key"]
        or isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or chat_id != identity["telegram_user_id"]
    ):
        raise ValueError(_DENIED)
