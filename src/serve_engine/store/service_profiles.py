from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field


class AlreadyExists(Exception):
    pass


@dataclass(frozen=True)
class ServiceProfile:
    id: int
    name: str
    model_name: str
    hf_repo: str
    revision: str
    backend: str
    image_tag: str
    gpu_ids: list[int]
    tensor_parallel: int
    max_model_len: int
    dtype: str
    pinned: bool
    idle_timeout_s: int | None
    target_concurrency: int | None
    max_loras: int
    max_lora_rank: int
    extra_args: dict[str, str] = field(default_factory=dict)
    # None / 'local' = leader. Otherwise the label of an enrolled agent
    # node — `deploy_profile(name)` resolves it to a node_id and routes
    # the engine container there.
    node_label: str | None = None


def _row_to_profile(row: sqlite3.Row) -> ServiceProfile:
    gpu_csv = row["gpu_ids"] or ""
    raw_extra = row["extra_args_json"] or "{}"
    try:
        extra = json.loads(raw_extra)
    except json.JSONDecodeError:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    try:
        node_label = row["node_label"]
    except (IndexError, KeyError):
        node_label = None
    return ServiceProfile(
        id=row["id"],
        name=row["name"],
        model_name=row["model_name"],
        hf_repo=row["hf_repo"],
        revision=row["revision"],
        backend=row["backend"],
        image_tag=row["image_tag"],
        gpu_ids=[int(x) for x in gpu_csv.split(",") if x],
        tensor_parallel=row["tensor_parallel"],
        max_model_len=row["max_model_len"],
        dtype=row["dtype"],
        pinned=bool(row["pinned"]),
        idle_timeout_s=row["idle_timeout_s"],
        target_concurrency=row["target_concurrency"],
        max_loras=row["max_loras"],
        max_lora_rank=row["max_lora_rank"],
        extra_args={str(k): str(v) for k, v in extra.items()},
        node_label=node_label,
    )


def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    model_name: str,
    hf_repo: str,
    revision: str,
    backend: str,
    image_tag: str,
    gpu_ids: list[int],
    tensor_parallel: int,
    max_model_len: int,
    dtype: str,
    pinned: bool = False,
    idle_timeout_s: int | None = None,
    target_concurrency: int | None = None,
    max_loras: int = 0,
    max_lora_rank: int = 0,
    extra_args: dict[str, str] | None = None,
    node_label: str | None = None,
) -> ServiceProfile:
    gpu_csv = ",".join(str(g) for g in gpu_ids)
    extra_json = json.dumps(extra_args or {}, sort_keys=True)
    try:
        cur = conn.execute(
            """
            INSERT INTO service_profiles (
                name, model_name, hf_repo, revision, backend, image_tag,
                gpu_ids, tensor_parallel, max_model_len, dtype, pinned,
                idle_timeout_s, target_concurrency, max_loras, max_lora_rank,
                extra_args_json, node_label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name, model_name, hf_repo, revision, backend, image_tag,
                gpu_csv, tensor_parallel, max_model_len, dtype, 1 if pinned else 0,
                idle_timeout_s, target_concurrency, max_loras, max_lora_rank,
                extra_json, node_label,
            ),
        )
    except sqlite3.IntegrityError as e:
        raise AlreadyExists(f"service profile {name!r} already exists") from e
    result = get_by_id(conn, int(cur.lastrowid or 0))
    assert result is not None
    return result


def get_by_id(conn: sqlite3.Connection, profile_id: int) -> ServiceProfile | None:
    row = conn.execute(
        "SELECT * FROM service_profiles WHERE id=?",
        (profile_id,),
    ).fetchone()
    return _row_to_profile(row) if row else None


def get_by_name(conn: sqlite3.Connection, name: str) -> ServiceProfile | None:
    row = conn.execute(
        "SELECT * FROM service_profiles WHERE name=?",
        (name,),
    ).fetchone()
    return _row_to_profile(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[ServiceProfile]:
    rows = conn.execute("SELECT * FROM service_profiles ORDER BY id").fetchall()
    return [_row_to_profile(r) for r in rows]


def delete(conn: sqlite3.Connection, profile_id: int) -> None:
    conn.execute("DELETE FROM service_profiles WHERE id=?", (profile_id,))
