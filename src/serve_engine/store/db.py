from __future__ import annotations

import fcntl
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, cast


class _PrefetchedCursor:
    """In-memory cursor returned by LockedConnection.execute().

    The underlying sqlite3 cursor is fully consumed inside the connection
    lock; this wrapper hands rows back to callers without touching the
    connection again. Forwards `lastrowid` and `rowcount` since several
    store-layer functions read them after INSERT/DELETE.
    """

    __slots__ = ("_idx", "_rows", "lastrowid", "rowcount")

    def __init__(self, rows: list, lastrowid: int | None, rowcount: int) -> None:
        self._rows = rows
        self._idx = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        r = self._rows[self._idx]
        self._idx += 1
        return r

    def fetchall(self):
        rest = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rest

    def __iter__(self):
        while self._idx < len(self._rows):
            yield self._rows[self._idx]
            self._idx += 1


class LockedConnection:
    """sqlite3.Connection wrapper that serializes access via an RLock.

    The daemon shares one long-lived connection across FastAPI sync deps that
    run in the anyio worker-thread pool. sqlite3 with `check_same_thread=False`
    permits cross-thread use but does not serialize - concurrent execute()
    calls corrupt cursor state, surfacing as 'bad parameter or other API
    misuse', empty rows, or NoneType subscript errors.

    Per-execute locking alone is insufficient: callers chain `.execute(...)
    .fetchone()`, and a different thread's execute() between those two calls
    can corrupt the live cursor. So execute() consumes the cursor fully
    inside the lock and returns an in-memory _PrefetchedCursor; subsequent
    fetchone()/fetchall() touch only Python lists, not the connection.

    For multi-statement atomic sections (SELECT-then-UPDATE), wrap the block
    in `with conn.locked():` so the pattern is one logical operation against
    other threads.

    The wrapper forwards less-common attribute access to the underlying
    connection, so callers can keep using `sqlite3.Connection`-typed signatures.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, *args: Any, **kwargs: Any) -> _PrefetchedCursor:
        with self._lock:
            cur = self._conn.execute(*args, **kwargs)
            rows = cur.fetchall()
            return _PrefetchedCursor(rows, cur.lastrowid, cur.rowcount)

    def executemany(self, *args: Any, **kwargs: Any) -> _PrefetchedCursor:
        with self._lock:
            cur = self._conn.executemany(*args, **kwargs)
            rows = cur.fetchall()
            return _PrefetchedCursor(rows, cur.lastrowid, cur.rowcount)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            return self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            return self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            return self._conn.close()

    @contextmanager
    def locked(self) -> Iterator[LockedConnection]:
        """Hold the connection lock for a multi-statement atomic section."""
        with self._lock:
            yield self

    def __getattr__(self, name: str) -> Any:
        # Fallback for less-common methods (interrupt, set_trace_callback, etc.)
        return getattr(self._conn, name)


def connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode, foreign keys, and autocommit.

    `isolation_level=None` puts the connection in autocommit mode: every DML
    statement commits immediately. This is necessary because the daemon
    shares a single long-lived connection across handlers that don't manage
    transactions explicitly - without autocommit, writes are lost on shutdown.

    Returns a `LockedConnection` (duck-typed as `sqlite3.Connection`) that
    serializes access across the FastAPI worker-thread pool.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA foreign_keys=ON")
    return cast(sqlite3.Connection, LockedConnection(raw))


@contextmanager
def locked(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Hold a LockedConnection lock when present.

    Tests and one-off tools may pass a plain sqlite3.Connection; in that
    case the context still works and simply yields the original connection.
    """
    lock = getattr(conn, "locked", None)
    if lock is None:
        yield conn
        return
    with lock() as locked_conn:
        yield locked_conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


class SchemaNewerThanBinary(RuntimeError):
    """Raised by init_schema when the DB has migration rows the binary
    doesn't know about — refusing to start prevents an older binary from
    silently running against a newer schema."""


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply pending migrations under a file-level advisory lock.

    Two daemon processes racing on the same DB serialize on the lock
    file next to db.sqlite — the second waits for the first to release
    before inspecting state. SQLite's BEGIN EXCLUSIVE isn't sufficient
    here because executescript implicitly COMMITs the active
    transaction before running its body, releasing the lock mid-flight.

    If the DB records migration filenames the running binary does not
    ship, it's older than the DB. We refuse to start rather than
    operate against an unknown future schema.
    """
    _ensure_migrations_table(conn)
    mig_dir = files("serve_engine.store.migrations")
    on_disk = {
        entry.name for entry in mig_dir.iterdir()
        if entry.name.endswith(".sql")
    }

    db_path = _connection_db_path(conn)
    lock_path = (
        db_path.with_suffix(db_path.suffix + ".migration.lock")
        if db_path
        else None
    )
    lock_handle = None
    if lock_path is not None:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = lock_path.open("w")
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
    try:
        # Forward-version safety: applied filenames the binary doesn't
        # ship. Check this inside the lock so a racing upgrade doesn't
        # invalidate our view between read and apply.
        rows = conn.execute("SELECT filename FROM _migrations").fetchall()
        applied = {r["filename"] for r in rows}
        unknown = applied - on_disk
        if unknown:
            raise SchemaNewerThanBinary(
                f"DB has migrations this binary doesn't ship: "
                f"{sorted(unknown)}. Likely a downgrade; refusing to "
                "start. Run the newer binary or restore from a backup."
            )
        for entry in sorted(mig_dir.iterdir(), key=lambda p: p.name):
            if not entry.name.endswith(".sql") or entry.name in applied:
                continue
            sql = entry.read_text()
            conn.executescript(sql)
            conn.execute(
                "INSERT OR IGNORE INTO _migrations (filename) VALUES (?)",
                (entry.name,),
            )
    finally:
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_handle.close()


def _connection_db_path(conn: sqlite3.Connection) -> Path | None:
    """Try to recover the on-disk path from a sqlite3 connection (or a
    LockedConnection wrapping one). Returns None for :memory: or when
    we can't introspect — in that case migrations run without the
    advisory lock (single-process tests)."""
    try:
        rows = conn.execute(
            "SELECT file FROM pragma_database_list() WHERE name='main'"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows or not rows[0]["file"]:
        return None
    return Path(rows[0]["file"])
