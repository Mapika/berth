from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from berth.store import db


class AlreadyExists(Exception):
    pass


class UnknownProfile(Exception):
    pass


@dataclass(frozen=True)
class ServiceRoute:
    id: int
    name: str
    match_model: str
    profile_id: int
    profile_name: str
    target_model_name: str
    fallback_profile_id: int | None
    fallback_profile_name: str | None
    fallback_model_name: str | None
    enabled: bool
    priority: int


def _profile_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM service_profiles WHERE name=?",
        (name,),
    ).fetchone()
    if row is None:
        raise UnknownProfile(f"service profile {name!r} not found")
    return int(row["id"])


def _row_to_route(row: sqlite3.Row) -> ServiceRoute:
    return ServiceRoute(
        id=row["id"],
        name=row["name"],
        match_model=row["match_model"],
        profile_id=row["profile_id"],
        profile_name=row["profile_name"],
        target_model_name=row["target_model_name"],
        fallback_profile_id=row["fallback_profile_id"],
        fallback_profile_name=row["fallback_profile_name"],
        fallback_model_name=row["fallback_model_name"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
    )


_SELECT = """
SELECT
    r.*,
    p.name AS profile_name,
    p.model_name AS target_model_name,
    fp.name AS fallback_profile_name,
    fp.model_name AS fallback_model_name
FROM service_routes r
JOIN service_profiles p ON p.id = r.profile_id
LEFT JOIN service_profiles fp ON fp.id = r.fallback_profile_id
"""


def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    match_model: str,
    profile_name: str,
    fallback_profile_name: str | None = None,
    enabled: bool = True,
    priority: int = 100,
) -> ServiceRoute:
    with db.locked(conn):
        profile_id = _profile_id(conn, profile_name)
        fallback_profile_id = (
            _profile_id(conn, fallback_profile_name)
            if fallback_profile_name is not None else None
        )
        try:
            cur = conn.execute(
                """
                INSERT INTO service_routes (
                    name, match_model, profile_id, fallback_profile_id,
                    enabled, priority
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    name, match_model, profile_id, fallback_profile_id,
                    1 if enabled else 0, priority,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise AlreadyExists(f"service route {name!r} already exists") from e
    result = get_by_id(conn, int(cur.lastrowid or 0))
    assert result is not None
    return result


def get_by_id(conn: sqlite3.Connection, route_id: int) -> ServiceRoute | None:
    row = conn.execute(
        _SELECT + " WHERE r.id=?",
        (route_id,),
    ).fetchone()
    return _row_to_route(row) if row else None


def get_by_name(conn: sqlite3.Connection, name: str) -> ServiceRoute | None:
    row = conn.execute(
        _SELECT + " WHERE r.name=?",
        (name,),
    ).fetchone()
    return _row_to_route(row) if row else None


def find_enabled_for_model(
    conn: sqlite3.Connection,
    match_model: str,
) -> ServiceRoute | None:
    row = conn.execute(
        _SELECT + """
        WHERE r.match_model=? AND r.enabled=1
        ORDER BY r.priority ASC, r.id ASC
        LIMIT 1
        """,
        (match_model,),
    ).fetchone()
    return _row_to_route(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[ServiceRoute]:
    rows = conn.execute(
        _SELECT + " ORDER BY r.priority ASC, r.id ASC",
    ).fetchall()
    return [_row_to_route(r) for r in rows]


def delete(conn: sqlite3.Connection, route_id: int) -> None:
    conn.execute("DELETE FROM service_routes WHERE id=?", (route_id,))
