"""Migration locking + forward-version safety."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from berth.store import db


def test_init_schema_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    db.init_schema(conn)  # second run is a no-op
    rows = conn.execute("SELECT COUNT(*) AS n FROM _migrations").fetchall()
    assert rows[0]["n"] > 0


def test_init_schema_raises_when_db_has_unknown_migrations(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    # Simulate a future binary having applied a migration this one doesn't ship.
    conn.execute(
        "INSERT INTO _migrations (filename) VALUES (?)",
        ("9999_from_the_future.sql",),
    )
    with pytest.raises(db.SchemaNewerThanBinary) as exc:
        db.init_schema(conn)
    assert "9999_from_the_future.sql" in str(exc.value)


def test_concurrent_init_schema_does_not_double_apply(tmp_path):
    """Two threads calling init_schema concurrently on separate
    connections to the same DB file both succeed; no migration is
    applied twice."""
    db_path = tmp_path / "t.db"
    errors: list[BaseException] = []

    def worker():
        try:
            conn = db.connect(db_path)
            db.init_schema(conn)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"thread errors: {errors}"

    conn = db.connect(db_path)
    rows = conn.execute(
        "SELECT filename, COUNT(*) AS n FROM _migrations "
        "GROUP BY filename HAVING n > 1"
    ).fetchall()
    assert rows == [], f"duplicate migration entries: {[dict(r) for r in rows]}"


def test_connect_serializes_wal_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    active_wal_pragmas = 0
    lock = threading.Lock()

    class FakeRawConnection:
        row_factory = None

        def execute(self, sql):
            nonlocal active_wal_pragmas
            if sql == "PRAGMA journal_mode=WAL":
                with lock:
                    active_wal_pragmas += 1
                try:
                    time.sleep(0.02)
                    with lock:
                        if active_wal_pragmas > 1:
                            raise sqlite3.OperationalError("database is locked")
                finally:
                    with lock:
                        active_wal_pragmas -= 1
            return self

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: FakeRawConnection())

    errors: list[BaseException] = []

    def worker():
        try:
            db.connect(db_path)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
