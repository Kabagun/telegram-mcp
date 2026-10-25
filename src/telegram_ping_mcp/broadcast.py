"""Silent release notifications with durable, content-free delivery records."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from .store import Store
from .telegram import TelegramAPIError, TelegramClient

_MAX_INTEGER = 2**63 - 1
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}\Z")
_SEND_INTERVAL_SECONDS = 0.1
_FAILED = "Release notification was rejected."
_UNKNOWN = "Release notification status is unknown."
_INELIGIBLE = "Release recipient is no longer eligible."


class BroadcastError(ValueError):
    """A content-free operator error."""


def _digest(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate(version: str, message: str, retry_failed: bool) -> None:
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise BroadcastError("Release version is invalid.")
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > 4096
        or any(
            unicodedata.category(char) == "Cs"
            or (unicodedata.category(char) == "Cc" and char not in "\n\t")
            for char in message
        )
        or len(message.encode("utf-16-le")) // 2 > 4096
    ):
        raise BroadcastError("Release message is invalid.")
    if not isinstance(retry_failed, bool):
        raise BroadcastError("Release retry policy is invalid.")


def _eligible(user: object) -> bool:
    return (
        isinstance(user, dict)
        and isinstance(user.get("user_key"), str)
        and bool(user["user_key"])
        and user.get("active") is True
        and isinstance(user.get("telegram_user_id"), int)
        and not isinstance(user["telegram_user_id"], bool)
        and 1 <= user["telegram_user_id"] <= _MAX_INTEGER
        and isinstance(user.get("chat_id"), int)
        and not isinstance(user["chat_id"], bool)
        and user["chat_id"] == user["telegram_user_id"]
    )


def _still_bound(store: Store, user: dict[str, Any]) -> bool:
    current = store.get_user(user["user_key"])
    return (
        _eligible(current)
        and current["user_key"] == user["user_key"]
        and current["telegram_user_id"] == user["telegram_user_id"]
        and current["chat_id"] == user["chat_id"]
    )


def _acknowledged(message: object, chat_id: int) -> bool:
    return (
        isinstance(message, dict)
        and isinstance(message.get("message_id"), int)
        and not isinstance(message["message_id"], bool)
        and 1 <= message["message_id"] <= _MAX_INTEGER
        and isinstance(message.get("chat"), dict)
        and isinstance(message["chat"].get("id"), int)
        and not isinstance(message["chat"]["id"], bool)
        and message["chat"]["id"] == chat_id
    )


async def broadcast_release(
    store: Store,
    bot: TelegramClient,
    *,
    version: str,
    message: str,
    retry_failed: bool = False,
    reply_markup_for: Callable[[int], dict] | None = None,
) -> dict[str, int]:
    """Send an approved release once per eligible binding, without storing text.

    Counts cover attempts claimed by this call. Existing sent, sending, unknown,
    and (by default) failed records are skipped. A version-only guard freezes the
    exact approved text before any recipients, including newly linked users.
    """
    _validate(version, message, retry_failed)
    guard_id = "release-message:" + _digest(version)
    guard_fingerprint = _digest({"kind": "release-message", "version": version, "text": message})
    try:
        guard = store.reserve_delivery(guard_id, guard_fingerprint)
    except ValueError:
        raise BroadcastError("Release message differs from the reserved version.") from None
    if guard["is_new"]:
        store.finish_delivery(guard_id, "sent")

    recipients = [user for user in store.list_users("", _MAX_INTEGER) if _eligible(user)]
    counts = {"sent": 0, "failed": 0}
    attempted = False
    for user in recipients:
        if not _still_bound(store, user):
            continue
        identity = {
            "version": version,
            "user_key": user["user_key"],
            "telegram_user_id": user["telegram_user_id"],
        }
        request_id = "release:" + _digest(identity)
        fingerprint = _digest({"kind": "release", **identity, "text": message})
        try:
            delivery = store.reserve_delivery(request_id, fingerprint, retry_failed=retry_failed)
        except ValueError:
            raise BroadcastError("Release message differs from the reserved version.") from None
        if not delivery["is_new"]:
            continue
        try:
            await bot.set_commands([], chat_id=user["chat_id"])
        except Exception:
            # No message has been sent yet, so an explicit failed retry is safe.
            store.finish_delivery(request_id, "failed", error="Release menu could not be updated.")
            counts["failed"] += 1
            continue
        try:
            if attempted:
                await asyncio.sleep(_SEND_INTERVAL_SECONDS)
            if not _still_bound(store, user):
                store.finish_delivery(request_id, "failed", error=_INELIGIBLE)
                counts["failed"] += 1
                continue
            attempted = True
            options: dict[str, Any] = {"disable_notification": True}
            if reply_markup_for is not None:
                options["reply_markup"] = reply_markup_for(user["chat_id"])
            result = await bot.send_message(user["chat_id"], message, **options)
        except TelegramAPIError as error:
            store.finish_delivery(request_id, "failed", error=_FAILED)
            if error.code == 403 and _still_bound(store, user):
                store.set_active_by_telegram(user["telegram_user_id"], False)
        except asyncio.CancelledError:
            store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
            raise
        except Exception:
            # Any unexpected send error may follow a delivery. Never reclaim it.
            store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
        else:
            if _acknowledged(result, user["chat_id"]):
                store.finish_delivery(request_id, "sent", message_id=result["message_id"])
                counts["sent"] += 1
                continue
            store.finish_delivery(request_id, "unknown", error=_UNKNOWN)
        counts["failed"] += 1
    return counts
