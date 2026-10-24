from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from telegram_ping_mcp.store import Store


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


@pytest.fixture
def store(database: Path) -> Store:
    return Store(database)


def link(store: Store, user_key: str = "example", telegram_id: int = 123) -> dict:
    invitation = store.create_link(user_key, user_key.title())
    return store.consume_link(invitation["token"], telegram_id, telegram_id, "example", "Example")


def fingerprint(value: str = "example") -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_links_are_verified_single_use_and_only_hash_is_persisted(
    store: Store, database: Path
) -> None:
    invitation = store.create_link("internal-key", "Example User")
    assert set(invitation) == {"token", "expires_at", "user_key"}
    assert len(invitation["token"]) <= 64
    assert store.get_user("internal-key") is None
    assert store.list_users() == []
    user = store.consume_link(invitation["token"], 123, 123, "example", "Example")
    assert user == {
        "user_key": "internal-key",
        "display_name": "Example User",
        "telegram_user_id": 123,
        "chat_id": 123,
        "username": "example",
        "first_name": "Example",
        "active": True,
    }
    with pytest.raises(ValueError, match="invalid or expired"):
        store.consume_link(invitation["token"], 123, 123, None, "Example")
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT token_hash FROM link_tokens").fetchone()
        assert row[0] == hashlib.sha256(invitation["token"].encode()).hexdigest()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert invitation["token"].encode() not in database.read_bytes()


def test_expiry_is_inclusive_and_a_replacement_revokes_previous_unused_link(database: Path) -> None:
    now = [1000.0]
    store = Store(database, now=lambda: now[0])
    old = store.create_link("example", "Example", ttl_seconds=10)
    assert old["expires_at"] == 1010
    replacement = store.create_link("example", "Renamed", ttl_seconds=20)
    with pytest.raises(ValueError, match="invalid or expired"):
        store.consume_link(old["token"], 123, 123, None, "Example")
    now[0] = 1020.0
    with pytest.raises(ValueError, match="invalid or expired"):
        store.consume_link(replacement["token"], 123, 123, None, "Example")
    assert store.get_user("example") is None


@pytest.mark.parametrize("telegram_id,chat_id", [(123, -123), (-123, -123), (123, 124), (0, 0)])
def test_link_requires_private_positive_identity(
    store: Store, telegram_id: int, chat_id: int
) -> None:
    invitation = store.create_link("example", "Example")
    with pytest.raises(ValueError):
        store.consume_link(invitation["token"], telegram_id, chat_id, None, "Example")
    assert store.get_user("example") is None
    assert store.consume_link(invitation["token"], 123, 123, None, "Example")["active"]


def test_conflicts_never_reassign_a_binding_or_burn_the_invitation(store: Store) -> None:
    link(store, "first", 123)
    invitation = store.create_link("second", "Second")
    with pytest.raises(ValueError, match="already linked"):
        store.consume_link(invitation["token"], 123, 123, None, "Example")
    assert store.consume_link(invitation["token"], 456, 456, None, "Other")["user_key"] == "second"
    same_key = store.create_link("first", "First renamed")
    with pytest.raises(ValueError, match="already linked"):
        store.consume_link(same_key["token"], 789, 789, None, "Third")
    assert store.get_user("first")["telegram_user_id"] == 123
    assert store.consume_link(same_key["token"], 123, 123, "newname", "New")["display_name"] == (
        "First renamed"
    )


def test_stop_and_relink_resubscribe_only_the_same_verified_identity(store: Store) -> None:
    link(store)
    assert store.get_user_by_telegram(123) == store.get_user("example")
    assert store.get_user_by_telegram(999) is None
    assert store.set_active_by_telegram(123, False)
    assert store.get_user("example")["active"] is False
    assert not store.set_active_by_telegram(999, False)
    assert link(store)["active"] is True
    assert store.set_active_by_telegram(123, True)
    assert store.get_user("example")["active"] is True


def test_search_is_literal_case_insensitive_and_covers_all_public_name_fields(store: Store) -> None:
    invitation = store.create_link("key_100%", "Иван Example")
    store.consume_link(invitation["token"], 123, 123, "UniqueHandle", "SpecialFirst")
    link(store, "another", 456)
    for query in ("KEY_100%", "ивАН", "uniquehandle", "specialFIRST"):
        assert [user["user_key"] for user in store.list_users(query)] == ["key_100%"]
    assert [user["user_key"] for user in store.list_users("%")] == ["key_100%"]
    assert store.list_users("missing") == []
    assert len(store.list_users(limit=1)) == 1
    assert store.list_users(limit=0) == []
    assert set(store.list_users()[0]) == {
        "user_key",
        "display_name",
        "telegram_user_id",
        "chat_id",
        "username",
        "first_name",
        "active",
    }


def test_delete_revokes_invites_and_keeps_delivery_ledger(store: Store, database: Path) -> None:
    link(store)
    invitation = store.create_link("example", "Example")
    store.reserve_delivery("request", fingerprint())
    store.finish_delivery("request", "sent", message_id=22)
    assert store.delete_user("example")
    assert store.get_user("example") is None
    assert not store.delete_user("example")
    with pytest.raises(ValueError, match="invalid or expired"):
        store.consume_link(invitation["token"], 123, 123, None, "Example")
    assert Store(database).reserve_delivery("request", fingerprint()) == {
        "status": "sent",
        "is_new": False,
        "message_id": 22,
    }
    pending = store.create_link("pending", "Pending")
    assert store.delete_user("pending")
    with pytest.raises(ValueError):
        store.consume_link(pending["token"], 123, 123, None, "Example")
    assert link(store, "new-key", 123)["active"]


def test_offsets_are_persistent_monotonic_and_safe_under_concurrent_updates(
    store: Store, database: Path
) -> None:
    assert store.get_offset() == 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store.set_offset, [3, 8, 2, 15, 4, 1, 11, 0]))
    assert Store(database).get_offset() == 15
    store.set_offset(14)
    assert store.get_offset() == 15
    with pytest.raises(ValueError):
        store.set_offset(-1)


def test_concurrent_delivery_reservations_have_exactly_one_winner(store: Store) -> None:
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(lambda _: store.reserve_delivery("request", fingerprint()), range(24))
        )
    assert sum(result["is_new"] for result in results) == 1
    assert all(result["status"] == "sending" for result in results)
    with pytest.raises(ValueError, match="different delivery"):
        store.reserve_delivery("request", fingerprint("different"))


@pytest.mark.parametrize("status", ["sending", "sent", "failed", "unknown"])
def test_delivery_status_survives_restart_without_reserving_again(
    store: Store, database: Path, status: str
) -> None:
    assert store.reserve_delivery("request", fingerprint()) == {"status": "sending", "is_new": True}
    details = {}
    if status != "sending":
        details = {"message_id": 42} if status == "sent" else {"error": "Safe failure summary"}
        store.finish_delivery("request", status, **details)
    assert Store(database).reserve_delivery("request", fingerprint()) == {
        "status": status,
        "is_new": False,
        **details,
    }


def test_link_consumption_is_atomic_between_independent_store_instances(database: Path) -> None:
    stores = [Store(database), Store(database)]
    invitation = stores[0].create_link("example", "Example")

    def consume(index: int) -> bool:
        try:
            stores[index % 2].consume_link(invitation["token"], 123, 123, None, "Example")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(consume, range(16)))
    assert sum(results) == 1


def test_invalid_arguments_raise_safe_errors_without_ids_or_tokens(store: Store) -> None:
    invitation = store.create_link("example", "Example")
    with pytest.raises(ValueError) as error:
        store.consume_link(invitation["token"], 123456, 654321, None, "Example")
    assert invitation["token"] not in str(error.value)
    assert "123456" not in str(error.value)
    assert "654321" not in str(error.value)
    with pytest.raises(ValueError):
        store.create_link("example", "Example", ttl_seconds=0)
    with pytest.raises(ValueError):
        store.reserve_delivery("request", "message content")
    with pytest.raises(ValueError):
        store.finish_delivery("missing", "sent")
    with pytest.raises(ValueError):
        store.finish_delivery("request", "sending")
