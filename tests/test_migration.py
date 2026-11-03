"""Consistent private snapshots without granting the deploy owner data access."""

import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

import telegram_ping_mcp.migration as migration
from telegram_ping_mcp.config import Config
from telegram_ping_mcp.inbox import InboxStore
from telegram_ping_mcp.migration import MigrationError, snapshot_history_migration
from telegram_ping_mcp.server import Runtime
from telegram_ping_mcp.store import Store


def legacy_database(path):
    Store(path)
    InboxStore(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE history_backfill_state")
        connection.execute("ALTER TABLE inbound_messages DROP COLUMN direction")
        connection.execute("CREATE TABLE synthetic_fixture (text TEXT)")
        connection.execute("INSERT INTO synthetic_fixture VALUES ('Synthetic private incoming')")
        connection.commit()


def test_fresh_and_current_databases_need_no_snapshot(tmp_path):
    path = tmp_path / "private.sqlite3"
    assert snapshot_history_migration(path) is None
    assert not path.exists()
    Store(path)
    InboxStore(path)
    assert snapshot_history_migration(path) is None
    assert not (tmp_path / "backups").exists()


def test_snapshot_contains_committed_wal_data_without_changing_schema(tmp_path):
    path = tmp_path / "private.sqlite3"
    legacy_database(path)
    with closing(sqlite3.connect(path)) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO synthetic_fixture VALUES ('Synthetic WAL row')")
        live.commit()
        snapshot = snapshot_history_migration(path)
        assert snapshot.parent == tmp_path / "backups"
        with closing(sqlite3.connect(snapshot)) as restored:
            assert restored.execute("SELECT text FROM synthetic_fixture").fetchall() == [
                ("Synthetic private incoming",),
                ("Synthetic WAL row",),
            ]
            assert "direction" not in {
                row[1] for row in restored.execute("PRAGMA table_info(inbound_messages)")
            }
            assert restored.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert live.execute("SELECT COUNT(*) FROM synthetic_fixture").fetchone() == (2,)
    if os.name != "nt":
        assert snapshot.stat().st_mode & 0o777 == 0o600
        assert snapshot.parent.stat().st_mode & 0o777 == 0o700


def test_failed_snapshot_never_removes_an_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "private.sqlite3"
    legacy_database(path)
    monkeypatch.setattr(migration, "uuid4", lambda: SimpleNamespace(hex="synthetic"))
    directory = tmp_path / "backups"
    directory.mkdir()
    existing = directory / "before-history-1.2.0-synthetic.sqlite3"
    existing.write_bytes(b"Synthetic unrelated existing file")
    with pytest.raises(MigrationError, match="protected database snapshot"):
        snapshot_history_migration(path)
    assert existing.read_bytes() == b"Synthetic unrelated existing file"


def test_timeout_removes_only_the_incomplete_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "private.sqlite3"
    legacy_database(path)
    clock = iter([0, 16])
    monkeypatch.setattr(migration, "monotonic", lambda: next(clock))
    with pytest.raises(MigrationError, match="protected database snapshot"):
        snapshot_history_migration(path)
    assert list((tmp_path / "backups").iterdir()) == []
    with closing(sqlite3.connect(path)) as original:
        assert original.execute("SELECT COUNT(*) FROM synthetic_fixture").fetchone() == (1,)


class Bot:
    async def get_me(self):
        return {"username": "synthetic_bot"}

    async def set_commands(self, commands):
        return True

    async def aclose(self):
        pass


async def test_runtime_snapshots_before_migration_and_skips_it_on_restart(tmp_path):
    path = tmp_path / "private.sqlite3"
    legacy_database(path)
    config = Config(database_path=path, polling_enabled=False)
    async with Runtime(config, bot=Bot()).lifespan():
        snapshots = list((tmp_path / "backups").iterdir())
        assert len(snapshots) == 1
        with closing(sqlite3.connect(snapshots[0])) as snapshot:
            assert "history_backfill_state" not in {
                row[0] for row in snapshot.execute("SELECT name FROM sqlite_master")
            }
        with closing(sqlite3.connect(path)) as updated:
            assert "direction" in {
                row[1] for row in updated.execute("PRAGMA table_info(inbound_messages)")
            }
    async with Runtime(config, bot=Bot()).lifespan():
        assert list((tmp_path / "backups").iterdir()) == snapshots


async def test_runtime_does_not_migrate_when_snapshot_fails(tmp_path, monkeypatch):
    path = tmp_path / "private.sqlite3"
    legacy_database(path)

    def deny(*args, **kwargs):
        raise PermissionError("Synthetic private failure")

    monkeypatch.setattr(migration.os, "open", deny)
    runtime = Runtime(Config(database_path=path, polling_enabled=False), bot=Bot())
    with pytest.raises(MigrationError, match="protected database snapshot"):
        async with runtime.lifespan():
            pytest.fail("Migration must not start without a snapshot")
    with closing(sqlite3.connect(path)) as original:
        assert "direction" not in {
            row[1] for row in original.execute("PRAGMA table_info(inbound_messages)")
        }
        assert "history_backfill_state" not in {
            row[0] for row in original.execute("SELECT name FROM sqlite_master")
        }
