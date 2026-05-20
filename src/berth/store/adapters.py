from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from berth.store import db
from berth.store import models as model_store
from berth.store.models import Model
from berth.store.rows import row_get


class AlreadyExists(Exception):
    pass


class NameCollision(Exception):
    """Adapter name collides with a base model (or vice versa).

    Adapters and base models share the routing namespace - clients say
    `model='x'` and the proxy resolves to whichever has that name. We
    refuse the registration up front rather than letting routing be
    ambiguous later.
    """


class BaseNotFound(Exception):
    pass


@dataclass(frozen=True)
class Adapter:
    id: int
    name: str
    base_model: Model
    hf_repo: str
    revision: str
    local_path: str | None
    size_mb: int | None
    created_at: str
    source_peer_id: str | None
    updated_at: str
    lora_rank: int | None = None


def _row_to_adapter(conn: sqlite3.Connection, row: sqlite3.Row) -> Adapter:
    base = model_store.get_by_id(conn, row["base_model_id"])
    if base is None:
        # Schema integrity should prevent this; surface loudly if it happens.
        raise RuntimeError(
            f"adapter {row['name']!r} references missing base model id={row['base_model_id']}"
        )
    lora_rank_value = row_get(row, "lora_rank")
    return Adapter(
        id=row["id"],
        name=row["name"],
        base_model=base,
        hf_repo=row["hf_repo"],
        revision=row["revision"],
        local_path=row["local_path"],
        size_mb=row["size_mb"],
        created_at=row["created_at"],
        source_peer_id=row["source_peer_id"],
        updated_at=row["updated_at"],
        lora_rank=lora_rank_value,
    )


def add(
    conn: sqlite3.Connection,
    *,
    name: str,
    base_model_name: str,
    hf_repo: str,
    revision: str = "main",
) -> Adapter:
    """Register an adapter. Raises:
    - NameCollision if `name` already exists in adapters OR models.
    - BaseNotFound if `base_model_name` doesn't resolve to a model.
    - AlreadyExists if a duplicate adapter slips through (UNIQUE constraint).
    """
    base = model_store.get_by_name(conn, base_model_name)
    if base is None:
        raise BaseNotFound(f"base model {base_model_name!r} not registered")
    # Disjoint-namespace check: adapter names and base-model names share
    # the routing namespace, so they must not collide.
    with db.locked(conn):
        if conn.execute("SELECT 1 FROM models WHERE name=?", (name,)).fetchone():
            raise NameCollision(
                f"name {name!r} is already used by a base model"
            )
        if conn.execute("SELECT 1 FROM adapters WHERE name=?", (name,)).fetchone():
            raise NameCollision(
                f"adapter {name!r} already exists"
            )
        try:
            cur = conn.execute(
                """
                INSERT INTO adapters (name, base_model_id, hf_repo, revision)
                VALUES (?, ?, ?, ?)
                """,
                (name, base.id, hf_repo, revision),
            )
        except sqlite3.IntegrityError as e:
            raise AlreadyExists(f"adapter {name!r} already exists") from e
        if cur.lastrowid is None:
            raise RuntimeError("adapter insert did not return a row id")
        new_id = cur.lastrowid
    fetched = get_by_id(conn, new_id)
    if fetched is None:
        raise RuntimeError(f"adapter insert returned missing row id={new_id}")
    return fetched


def get_by_name(conn: sqlite3.Connection, name: str) -> Adapter | None:
    row = conn.execute("SELECT * FROM adapters WHERE name=?", (name,)).fetchone()
    return _row_to_adapter(conn, row) if row else None


def get_by_id(conn: sqlite3.Connection, adapter_id: int) -> Adapter | None:
    row = conn.execute("SELECT * FROM adapters WHERE id=?", (adapter_id,)).fetchone()
    return _row_to_adapter(conn, row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Adapter]:
    rows = conn.execute("SELECT * FROM adapters ORDER BY id").fetchall()
    return [_row_to_adapter(conn, r) for r in rows]


def list_for_base(conn: sqlite3.Connection, base_model_id: int) -> list[Adapter]:
    rows = conn.execute(
        "SELECT * FROM adapters WHERE base_model_id=? ORDER BY id",
        (base_model_id,),
    ).fetchall()
    return [_row_to_adapter(conn, r) for r in rows]


def set_local_path(conn: sqlite3.Connection, adapter_id: int, path: str) -> None:
    conn.execute(
        "UPDATE adapters SET local_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (path, adapter_id),
    )


def set_size_mb(conn: sqlite3.Connection, adapter_id: int, size_mb: int) -> None:
    conn.execute(
        "UPDATE adapters SET size_mb=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (size_mb, adapter_id),
    )


def set_lora_rank(conn: sqlite3.Connection, adapter_id: int, lora_rank: int) -> None:
    """Set the PEFT `r` value (LoRA rank) parsed from adapter_config.json."""
    conn.execute(
        "UPDATE adapters SET lora_rank=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (lora_rank, adapter_id),
    )


def delete(conn: sqlite3.Connection, adapter_id: int) -> None:
    conn.execute("DELETE FROM adapters WHERE id=?", (adapter_id,))
