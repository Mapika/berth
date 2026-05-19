from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from serve_engine.store.rows import row_get

Status = Literal["pending", "loading", "ready", "stopping", "stopped", "failed"]
ACTIVE_STATUSES: tuple[Status, ...] = ("pending", "loading", "ready")


@dataclass(frozen=True)
class Deployment:
    id: int
    model_id: int
    backend: str
    image_tag: str
    gpu_ids: list[int]
    tensor_parallel: int
    max_model_len: int | None
    dtype: str
    container_id: str | None
    container_name: str | None
    container_port: int | None
    container_address: str | None
    status: Status
    last_error: str | None
    pinned: bool
    idle_timeout_s: int | None
    vram_reserved_mb: int
    last_request_at: str | None
    max_loras: int = 0  # 0 = LoRA disabled
    max_lora_rank: int = 0  # 0 = unset; treat as engine default (16)
    image_digest: str | None = None  # docker image content-id (sha256:...)
    node_id: int = 0  # which cluster node owns this deployment (migration 014)


def _row_to_dep(row: sqlite3.Row) -> Deployment:
    gpu_csv = row["gpu_ids"] or ""
    gpu_ids = [int(x) for x in gpu_csv.split(",") if x]
    max_loras_value = row_get(row, "max_loras", 0)
    max_lora_rank_value = row_get(row, "max_lora_rank", 0)
    image_digest_value = row_get(row, "image_digest")
    node_id_value = row_get(row, "node_id", 0)
    return Deployment(
        id=row["id"],
        model_id=row["model_id"],
        backend=row["backend"],
        image_tag=row["image_tag"],
        gpu_ids=gpu_ids,
        tensor_parallel=row["tensor_parallel"],
        max_model_len=row["max_model_len"],
        dtype=row["dtype"],
        container_id=row["container_id"],
        container_name=row["container_name"],
        container_port=row["container_port"],
        container_address=row["container_address"],
        status=row["status"],
        last_error=row["last_error"],
        pinned=bool(row["pinned"]),
        idle_timeout_s=row["idle_timeout_s"],
        vram_reserved_mb=row["vram_reserved_mb"],
        last_request_at=row["last_request_at"],
        max_loras=max_loras_value or 0,
        max_lora_rank=max_lora_rank_value or 0,
        image_digest=image_digest_value,
        node_id=int(node_id_value or 0),
    )


def create(
    conn: sqlite3.Connection,
    *,
    model_id: int,
    backend: str,
    image_tag: str,
    gpu_ids: list[int],
    tensor_parallel: int,
    max_model_len: int | None,
    dtype: str,
    pinned: bool = False,
    idle_timeout_s: int | None = None,
    vram_reserved_mb: int = 0,
    max_loras: int = 0,
    max_lora_rank: int = 0,
) -> Deployment:
    gpu_csv = ",".join(str(g) for g in gpu_ids)
    cur = conn.execute(
        """
        INSERT INTO deployments
            (model_id, backend, image_tag, gpu_ids, tensor_parallel,
             max_model_len, dtype, pinned, idle_timeout_s, vram_reserved_mb,
             max_loras, max_lora_rank)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model_id, backend, image_tag, gpu_csv, tensor_parallel,
            max_model_len, dtype,
            1 if pinned else 0, idle_timeout_s, vram_reserved_mb,
            max_loras, max_lora_rank,
        ),
    )
    assert cur.lastrowid is not None
    result = get_by_id(conn, cur.lastrowid)
    assert result is not None
    return result


def get_by_id(conn: sqlite3.Connection, dep_id: int) -> Deployment | None:
    row = conn.execute("SELECT * FROM deployments WHERE id=?", (dep_id,)).fetchone()
    return _row_to_dep(row) if row else None


def update_status(
    conn: sqlite3.Connection,
    dep_id: int,
    status: Status,
    *,
    last_error: str | None = None,
) -> None:
    if last_error is not None:
        conn.execute(
            "UPDATE deployments SET status=?, last_error=? WHERE id=?",
            (status, last_error, dep_id),
        )
    else:
        conn.execute("UPDATE deployments SET status=? WHERE id=?", (status, dep_id))


def set_container(
    conn: sqlite3.Connection,
    dep_id: int,
    *,
    container_id: str,
    container_name: str,
    container_port: int,
    container_address: str,
    node_id: int | None = None,
) -> None:
    if node_id is None:
        conn.execute(
            """
            UPDATE deployments
            SET container_id=?, container_name=?, container_port=?, container_address=?,
                started_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (container_id, container_name, container_port, container_address, dep_id),
        )
    else:
        conn.execute(
            """
            UPDATE deployments
            SET container_id=?, container_name=?, container_port=?, container_address=?,
                node_id=?, started_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (container_id, container_name, container_port, container_address,
             node_id, dep_id),
        )


def find_active(conn: sqlite3.Connection) -> Deployment | None:
    placeholders = ",".join(["?"] * len(ACTIVE_STATUSES))
    row = conn.execute(
        f"SELECT * FROM deployments WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        ACTIVE_STATUSES,
    ).fetchone()
    return _row_to_dep(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Deployment]:
    rows = conn.execute("SELECT * FROM deployments ORDER BY id").fetchall()
    return [_row_to_dep(r) for r in rows]


def find_ready_by_model_name(conn: sqlite3.Connection, model_name: str) -> Deployment | None:
    """Return the most-recently-loaded ready deployment for a model, or None."""
    row = conn.execute(
        """
        SELECT d.* FROM deployments d
        JOIN models m ON m.id = d.model_id
        WHERE m.name = ? AND d.status = 'ready'
        ORDER BY d.started_at DESC LIMIT 1
        """,
        (model_name,),
    ).fetchone()
    return _row_to_dep(row) if row else None


def list_ready(conn: sqlite3.Connection) -> list[Deployment]:
    """All deployments currently in 'ready' status."""
    rows = conn.execute(
        "SELECT * FROM deployments WHERE status = 'ready' ORDER BY id"
    ).fetchall()
    return [_row_to_dep(r) for r in rows]


def list_evictable(conn: sqlite3.Connection) -> list[Deployment]:
    """Non-pinned ready deployments, sorted oldest-touched first (LRU)."""
    rows = conn.execute(
        """
        SELECT * FROM deployments
        WHERE status = 'ready' AND pinned = 0
        ORDER BY COALESCE(last_request_at, started_at) ASC
        """
    ).fetchall()
    return [_row_to_dep(r) for r in rows]


def touch_last_request(conn: sqlite3.Connection, dep_id: int) -> None:
    """Update last_request_at to now. Called by the proxy on every request."""
    conn.execute(
        "UPDATE deployments SET last_request_at = CURRENT_TIMESTAMP WHERE id = ?",
        (dep_id,),
    )


def set_pinned(conn: sqlite3.Connection, dep_id: int, pinned: bool) -> None:
    conn.execute(
        "UPDATE deployments SET pinned = ? WHERE id = ?",
        (1 if pinned else 0, dep_id),
    )


def set_image_digest(conn: sqlite3.Connection, dep_id: int, digest: str) -> None:
    """Record the docker image content-id captured at container start.

    The tag in `image_tag` (e.g. `vllm/vllm-openai:v0.20.2`) is a mutable
    pointer - if upstream retags, reproducibility is lost. The digest is
    the immutable identifier for what was actually run.
    """
    conn.execute(
        "UPDATE deployments SET image_digest = ? WHERE id = ?",
        (digest, dep_id),
    )
