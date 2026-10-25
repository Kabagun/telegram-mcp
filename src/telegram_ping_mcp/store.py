"""Small SQLite persistence layer for verified links and delivery idempotency."""

from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_USER_COLUMNS = "user_key, display_name, telegram_user_id, chat_id, username, first_name, active"
_DELIVERY_STATUSES = {"sent", "failed", "unknown"}


class Store:
    """Use short transactions and a separate connection for each operation.

    ``now`` supplies UTC Unix seconds. No invitation tokens or message bodies are
    persisted: links store token hashes and deliveries store content fingerprints.
    """

    def __init__(self, path: str | Path, now: Callable[[], float] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or time.time
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    telegram_user_id INTEGER NOT NULL UNIQUE CHECK (telegram_user_id > 0),
                    chat_id INTEGER NOT NULL CHECK (chat_id = telegram_user_id),
                    username TEXT,
                    first_name TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS link_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL
                );
                CREATE INDEX IF NOT EXISTS link_tokens_user_key ON link_tokens (user_key);
                CREATE TABLE IF NOT EXISTS update_offset (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    next_offset INTEGER NOT NULL CHECK (next_offset >= 0)
                );
                INSERT OR IGNORE INTO update_offset VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS deliveries (
                    request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'failed', 'unknown')),
                    message_id INTEGER,
                    error TEXT
                );
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.create_function(
            "casefold", 1, lambda value: (value or "").casefold(), deterministic=True
        )
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _text(value: str, label: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a nonempty string.")

    @staticmethod
    def _integer(value: int, label: str, minimum: int = 0) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{label} is invalid.")
        if value > 2**63 - 1:
            raise ValueError(f"{label} is invalid.")

    @staticmethod
    def _user(row: sqlite3.Row) -> dict[str, Any]:
        user = dict(row)
        user["active"] = bool(user["active"])
        return user

    def create_link(
        self, user_key: str, display_name: str, ttl_seconds: int = 900
    ) -> dict[str, Any]:
        self._text(user_key, "User key")
        self._text(display_name, "Display name")
        self._integer(ttl_seconds, "Link lifetime", minimum=1)
        expires_at = self._now() + ttl_seconds
        if not math.isfinite(expires_at):
            raise ValueError("Link expiration is invalid.")
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM link_tokens WHERE user_key = ? AND consumed_at IS NULL", (user_key,)
            )
            while True:
                token = secrets.token_urlsafe(24)
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                if not connection.execute(
                    "SELECT 1 FROM link_tokens WHERE token_hash = ?", (token_hash,)
                ).fetchone():
                    break
            connection.execute(
                """INSERT INTO link_tokens (token_hash, user_key, display_name, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (token_hash, user_key, display_name, expires_at),
            )
        return {"token": token, "expires_at": expires_at, "user_key": user_key}

    def ensure_private_user(
        self,
        telegram_user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str,
    ) -> dict[str, Any]:
        """Enroll a private identity once, preserving any existing binding and state."""
        self._integer(telegram_user_id, "Private chat identity", minimum=1)
        self._integer(chat_id, "Private chat identity", minimum=1)
        if telegram_user_id != chat_id:
            raise ValueError("A private chat is required.")
        if username is not None and not isinstance(username, str):
            raise ValueError("Username is invalid.")
        if not isinstance(first_name, str):
            raise ValueError("First name is invalid.")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            if row is not None:
                return self._user(row)
            while True:
                user_key = "tg_" + secrets.token_urlsafe(24)
                if not connection.execute(
                    """SELECT 1 FROM users WHERE user_key = ?
                       UNION ALL SELECT 1 FROM link_tokens WHERE user_key = ? LIMIT 1""",
                    (user_key, user_key),
                ).fetchone():
                    break
            display_name = first_name.strip() or (username or "").strip() or "Telegram user"
            connection.execute(
                """INSERT INTO users
                   (user_key, display_name, telegram_user_id, chat_id, username, first_name, active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (user_key, display_name, telegram_user_id, chat_id, username, first_name),
            )
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE user_key = ?", (user_key,)
            ).fetchone()
            return self._user(row)

    def consume_link(
        self,
        token: str,
        telegram_user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str,
    ) -> dict[str, Any]:
        self._integer(telegram_user_id, "Private chat identity", minimum=1)
        self._integer(chat_id, "Private chat identity", minimum=1)
        if telegram_user_id != chat_id:
            raise ValueError("A private chat is required.")
        if not isinstance(token, str) or not token or len(token) > 64:
            raise ValueError("Invitation is invalid or expired.")
        if username is not None and not isinstance(username, str):
            raise ValueError("Username is invalid.")
        if not isinstance(first_name, str):
            raise ValueError("First name is invalid.")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = self._now()
        with self._transaction() as connection:
            link = connection.execute(
                "SELECT * FROM link_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if link is None or link["consumed_at"] is not None or link["expires_at"] <= now:
                raise ValueError("Invitation is invalid or expired.")
            binding = connection.execute(
                "SELECT telegram_user_id FROM users WHERE user_key = ?", (link["user_key"],)
            ).fetchone()
            if binding is not None and binding["telegram_user_id"] != telegram_user_id:
                raise ValueError("User is already linked to another Telegram identity.")
            identity = connection.execute(
                "SELECT user_key FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
            if identity is not None and identity["user_key"] != link["user_key"]:
                raise ValueError("Telegram identity is already linked to another user.")
            connection.execute(
                """INSERT INTO users
                   (user_key, display_name, telegram_user_id, chat_id, username, first_name, active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(user_key) DO UPDATE SET
                       display_name = excluded.display_name,
                       chat_id = excluded.chat_id,
                       username = excluded.username,
                       first_name = excluded.first_name,
                       active = 1""",
                (
                    link["user_key"],
                    link["display_name"],
                    telegram_user_id,
                    chat_id,
                    username,
                    first_name,
                ),
            )
            connection.execute(
                "UPDATE link_tokens SET consumed_at = ? WHERE token_hash = ?", (now, token_hash)
            )
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE user_key = ?", (link["user_key"],)
            ).fetchone()
            return self._user(row)

    def get_user(self, user_key: str) -> dict[str, Any] | None:
        self._text(user_key, "User key")
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE user_key = ?", (user_key,)
            ).fetchone()
            return self._user(row) if row is not None else None

    def get_user_by_telegram(self, telegram_user_id: int) -> dict[str, Any] | None:
        self._integer(telegram_user_id, "Telegram identity", minimum=1)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
            return self._user(row) if row is not None else None

    def list_users(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if not isinstance(query, str):
            raise ValueError("Search query is invalid.")
        self._integer(limit, "Search limit")
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT {_USER_COLUMNS} FROM users
                    WHERE instr(casefold(user_key), casefold(?)) > 0
                       OR instr(casefold(display_name), casefold(?)) > 0
                       OR instr(casefold(username), casefold(?)) > 0
                       OR instr(casefold(first_name), casefold(?)) > 0
                    ORDER BY casefold(display_name), user_key LIMIT ?""",
                (query, query, query, query, limit),
            ).fetchall()
            return [self._user(row) for row in rows]

    def set_active_by_telegram(self, telegram_user_id: int, active: bool) -> bool:
        self._integer(telegram_user_id, "Telegram identity", minimum=1)
        if not isinstance(active, bool):
            raise ValueError("Active state is invalid.")
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE users SET active = ? WHERE telegram_user_id = ?",
                (int(active), telegram_user_id),
            )
            return bool(cursor.rowcount)

    def delete_user(self, user_key: str) -> bool:
        self._text(user_key, "User key")
        with self._transaction() as connection:
            tokens = connection.execute("DELETE FROM link_tokens WHERE user_key = ?", (user_key,))
            users = connection.execute("DELETE FROM users WHERE user_key = ?", (user_key,))
            return bool(tokens.rowcount or users.rowcount)

    def get_offset(self) -> int:
        with self._connection() as connection:
            return connection.execute(
                "SELECT next_offset FROM update_offset WHERE singleton = 1"
            ).fetchone()[0]

    def set_offset(self, next_offset: int) -> None:
        self._integer(next_offset, "Update offset")
        with self._transaction() as connection:
            connection.execute(
                "UPDATE update_offset SET next_offset = max(next_offset, ?) WHERE singleton = 1",
                (next_offset,),
            )

    @staticmethod
    def _delivery(row: sqlite3.Row, *, is_new: bool) -> dict[str, Any]:
        result: dict[str, Any] = {"status": row["status"], "is_new": is_new}
        if row["message_id"] is not None:
            result["message_id"] = row["message_id"]
        if row["error"] is not None:
            result["error"] = row["error"]
        return result

    def reserve_delivery(self, request_id: str, fingerprint: str) -> dict[str, Any]:
        self._text(request_id, "Request ID")
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in fingerprint)
        ):
            raise ValueError("Delivery fingerprint must be a SHA-256 digest.")
        fingerprint = fingerprint.lower()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("Request ID was already used for a different delivery.")
                return self._delivery(row, is_new=False)
            connection.execute(
                "INSERT INTO deliveries (request_id, fingerprint, status) VALUES (?, ?, 'sending')",
                (request_id, fingerprint),
            )
            return {"status": "sending", "is_new": True}

    def finish_delivery(
        self,
        request_id: str,
        status: str,
        message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        self._text(request_id, "Request ID")
        if not isinstance(status, str) or status not in _DELIVERY_STATUSES:
            raise ValueError("Delivery status is invalid.")
        if message_id is not None:
            self._integer(message_id, "Message ID", minimum=1)
        if error is not None and not isinstance(error, str):
            raise ValueError("Delivery error is invalid.")
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE deliveries SET status = ?, message_id = ?, error = ? WHERE request_id = ?",
                (status, message_id, error, request_id),
            )
            if not cursor.rowcount:
                raise ValueError("Delivery reservation does not exist.")
