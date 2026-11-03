"""Private runtime-owned snapshots before the sent-history schema migration."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic
from uuid import uuid4


class MigrationError(RuntimeError):
    """A safe startup failure when the required snapshot cannot be created."""


def snapshot_history_migration(database_path: Path) -> Path | None:
    """Snapshot an existing legacy database before any new schema is installed.

    Run as the application runtime: deployment owners need no access to private
    configuration or messages. Never overwrite a snapshot or copy a live WAL file.
    """
    if not database_path.exists():
        return None
    snapshot = None
    created = False
    failed = False
    try:
        with closing(
            sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as source:
            tables = {
                row[0]
                for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not tables:
                return None
            columns = {row[1] for row in source.execute("PRAGMA table_info(inbound_messages)")}
            if "history_backfill_state" in tables and (
                "inbound_messages" not in tables or "direction" in columns
            ):
                return None
            directory = database_path.parent / "backups"
            if directory.is_symlink():
                raise MigrationError()
            directory.mkdir(mode=0o700, exist_ok=True)
            snapshot = directory / f"before-history-1.2.0-{uuid4().hex}.sqlite3"
            descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            os.close(descriptor)
            deadline = monotonic() + 15

            def progress(status: int, remaining: int, total: int) -> None:
                if monotonic() >= deadline:
                    raise MigrationError()

            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target, pages=128, progress=progress, sleep=0.05)
                if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise MigrationError()
    except (OSError, sqlite3.Error, MigrationError):
        failed = True
    if failed:
        if created:
            # Remove only this invocation's uniquely named incomplete snapshot.
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass
        raise MigrationError("A protected database snapshot is required before migration.")
    return snapshot
