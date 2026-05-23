from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from berth.store.rows import row_get

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
    source: str = "managed"  # 'managed' | 'adopted' (migration 016)


def _row_to_dep(row: sqlite3.Row) -> Deployment:
    gpu_csv = row["gpu_ids"] or ""
    gpu_ids = [int(x) for x in gpu_csv.split(",") if x]
    max_loras_value = row_get(row, "max_loras", 0)
    max_lora_rank_value = row_get(row, "max_lora_rank", 0)
    image_digest_value = row_get(row, "image_digest")
    node_id_value = row_get(row, "node_id", 0)
    source_value = row_get(row, "source", "managed")
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
        source=source_value or "managed",
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
    if cur.lastrowid is None:
        raise RuntimeError("deployment insert did not return a row id")
    result = get_by_id(conn, cur.lastrowid)
    if result is None:
        raise RuntimeError(f"deployment insert returned missing row id={cur.lastrowid}")
    return result


def get_by_id(conn: sqlite3.Connection, dep_id: int) -> Deployment | None:
    row = conn.execute("SELECT * FROM deployments WHERE id=?", (dep_id,)).fetchone()
    return _row_to_dep(row) if row else None


_TERMINAL_OR_TRANSITIONAL: tuple[Status, ...] = ("stopping", "stopped", "failed")


def update_status(
    conn: sqlite3.Connection,
    dep_id: int,
    status: Status,
    *,
    last_error: str | None = None,
) -> bool:
    """Update the deployment's status; returns True if the row was updated.

    Asymmetric: transitions *into* a terminal/transitional state
    (``stopping`` / ``stopped`` / ``failed``) are always allowed — operators
    must be able to force-stop or mark-failed at any time. Transitions back
    *into* an active state (``pending`` / ``loading`` / ``ready``) are
    refused if the row is already terminal. Without this guard, a late
    ``ready`` transition from a load() that raced a concurrent ``stop()``
    revives a deployment whose container has already been torn down; live
    traffic then routes to a dead engine until HealthMonitor catches up.
    """
    if status in _TERMINAL_OR_TRANSITIONAL:
        # Unconditional — stop_all etc. must always be able to clobber.
        if last_error is not None:
            cur = conn.execute(
                "UPDATE deployments SET status=?, last_error=? WHERE id=?",
                (status, last_error, dep_id),
            )
        else:
            cur = conn.execute(
                "UPDATE deployments SET status=? WHERE id=?",
                (status, dep_id),
            )
    elif last_error is not None:
        cur = conn.execute(
            "UPDATE deployments SET status=?, last_error=? "
            "WHERE id=? AND status NOT IN ('stopped', 'failed')",
            (status, last_error, dep_id),
        )
    else:
        cur = conn.execute(
            "UPDATE deployments SET status=? "
            "WHERE id=? AND status NOT IN ('stopped', 'failed')",
            (status, dep_id),
        )
    return (cur.rowcount or 0) > 0


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
        f"SELECT * FROM deployments WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT 1",  # nosec
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


def upsert_adopted(
    conn: sqlite3.Connection,
    *,
    model_id: int,
    node_id: int,
    container_id: str,
    address: str,
    port: int,
    gpu_ids: list[int],
    vram_reserved_mb: int,
    image_tag: str,
    status: Status = "ready",
) -> Deployment:
    """Create or update the adopted deployment for (node_id, container_id).

    Keyed on (node_id, container_id) so a repeated full-state report updates
    the existing row instead of duplicating it."""
    gpu_csv = ",".join(str(g) for g in gpu_ids)
    # Adopted rows have no separate human-friendly name, so container_name
    # deliberately mirrors container_id (in both the UPDATE and INSERT paths
    # below). This is intentional, not a copy-paste bug.
    existing = conn.execute(
        "SELECT id FROM deployments "
        "WHERE source='adopted' AND node_id=? AND container_id=?",
        (node_id, container_id),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE deployments
            SET model_id=?, backend='adopted', image_tag=?, gpu_ids=?,
                tensor_parallel=?, max_model_len=NULL, dtype='auto',
                container_name=?, container_port=?, container_address=?,
                vram_reserved_mb=?, status=?, last_error=NULL
            WHERE id=?
            """,
            (model_id, image_tag, gpu_csv, max(1, len(gpu_ids)),
             container_id, port, address, vram_reserved_mb, status,
             existing["id"]),
        )
        dep_id = existing["id"]
    else:
        cur = conn.execute(
            """
            INSERT INTO deployments
                (model_id, backend, image_tag, gpu_ids, tensor_parallel,
                 max_model_len, dtype, pinned, idle_timeout_s,
                 vram_reserved_mb, node_id, source,
                 container_id, container_name, container_port,
                 container_address, status)
            VALUES (?, 'adopted', ?, ?, ?, NULL, 'auto', 0, NULL, ?, ?,
                    'adopted', ?, ?, ?, ?, ?)
            """,
            (model_id, image_tag, gpu_csv, max(1, len(gpu_ids)),
             vram_reserved_mb, node_id, container_id, container_id,
             port, address, status),
        )
        if cur.lastrowid is None:
            raise RuntimeError("adopted deployment insert returned no id")
        dep_id = cur.lastrowid
    result = get_by_id(conn, dep_id)
    if result is None:
        raise RuntimeError(f"adopted upsert lost row id={dep_id}")
    return result


def list_adopted_for_node(
    conn: sqlite3.Connection, node_id: int
) -> list[Deployment]:
    rows = conn.execute(
        "SELECT * FROM deployments WHERE source='adopted' AND node_id=? "
        "ORDER BY id",
        (node_id,),
    ).fetchall()
    return [_row_to_dep(r) for r in rows]


def delete_adopted(conn: sqlite3.Connection, dep_id: int) -> None:
    conn.execute(
        "DELETE FROM deployments WHERE id=? AND source='adopted'", (dep_id,)
    )
