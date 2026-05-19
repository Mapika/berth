from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from berth.store import db
from berth.store.rows import row_get


@dataclass(frozen=True)
class ApiKey:
    id: int
    name: str
    prefix: str
    tier: str
    rpm_override: int | None
    tpm_override: int | None
    rpd_override: int | None
    tpd_override: int | None
    rph_override: int | None
    tph_override: int | None
    rpw_override: int | None
    tpw_override: int | None
    revoked_at: str | None
    allowed_models: list[str] | None = None
    usage_event_id: int | None = None


# Module-level pepper state. configure_pepper() loads (or mints) a 32-byte
# secret from disk at daemon startup; from then on _hash uses HMAC-SHA256
# with that pepper rather than plain SHA-256. Tests that don't configure
# a pepper stay on legacy SHA-256 — keeps the suite running unchanged.
_PEPPER_PATH: Path | None = None
_PEPPER_CACHED: bytes | None = None


def configure_pepper(path: Path) -> None:
    """Point the key-hash pepper at a file. Mints a fresh 32-byte secret
    at the path (mode 0600) on first call if absent. Idempotent."""
    global _PEPPER_PATH, _PEPPER_CACHED
    _PEPPER_PATH = path
    _PEPPER_CACHED = None  # force reload via _get_pepper on next call


def _get_pepper() -> bytes:
    """Returns the pepper bytes, loading or creating the file lazily.
    Returns empty bytes when no pepper is configured (legacy mode)."""
    global _PEPPER_CACHED
    if _PEPPER_CACHED is not None:
        return _PEPPER_CACHED
    if _PEPPER_PATH is None:
        return b""
    if not _PEPPER_PATH.exists():
        _PEPPER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PEPPER_PATH.write_bytes(secrets.token_bytes(32))
        try:
            _PEPPER_PATH.chmod(0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX modes
    _PEPPER_CACHED = _PEPPER_PATH.read_bytes()
    return _PEPPER_CACHED


def _hash(secret: str) -> str:
    pepper = _get_pepper()
    if not pepper:
        return hashlib.sha256(secret.encode()).hexdigest()
    return hmac.new(pepper, secret.encode(), hashlib.sha256).hexdigest()


def _decode_allowed_models(raw: object) -> list[str] | None:
    """NULL and empty string both map to None (unrestricted).

    Empty JSON list `[]` decodes to [] and is preserved as "restrict-all"
    (no model is allowed). Malformed JSON is treated as None defensively;
    the column is operator-managed and a bad value shouldn't lock the key
    out entirely, but we still log nothing here - the proxy will allow.
    """
    if raw is None or raw == "":
        return None
    try:
        decoded = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list):
        return None
    return [str(x) for x in decoded]


def _row_to_key(row: sqlite3.Row) -> ApiKey:
    raw_allow = row_get(row, "allowed_models")
    return ApiKey(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        tier=row["tier"],
        rpm_override=row["rpm_override"],
        tpm_override=row["tpm_override"],
        rpd_override=row["rpd_override"],
        tpd_override=row["tpd_override"],
        rph_override=row["rph_override"],
        tph_override=row["tph_override"],
        rpw_override=row["rpw_override"],
        tpw_override=row["tpw_override"],
        revoked_at=row["revoked_at"],
        allowed_models=_decode_allowed_models(raw_allow),
    )


def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    tier: str = "standard",
    rpm_override: int | None = None,
    tpm_override: int | None = None,
    rpd_override: int | None = None,
    tpd_override: int | None = None,
    rph_override: int | None = None,
    tph_override: int | None = None,
    rpw_override: int | None = None,
    tpw_override: int | None = None,
    allowed_models: list[str] | None = None,
) -> tuple[str, ApiKey]:
    """Generate a new key. Returns (secret, ApiKey). The secret is only available here."""
    body = secrets.token_urlsafe(32)
    secret = f"sk-{body}"
    prefix = secret[:12]
    key_hash = _hash(secret)
    allowed_json = (
        json.dumps(list(allowed_models)) if allowed_models is not None else None
    )
    cur = conn.execute(
        """
        INSERT INTO api_keys
            (name, prefix, key_hash, tier,
             rpm_override, tpm_override, rpd_override, tpd_override,
             rph_override, tph_override, rpw_override, tpw_override,
             allowed_models)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name, prefix, key_hash, tier,
            rpm_override, tpm_override, rpd_override, tpd_override,
            rph_override, tph_override, rpw_override, tpw_override,
            allowed_json,
        ),
    )
    assert cur.lastrowid is not None
    fetched = get_by_id(conn, cur.lastrowid)
    assert fetched is not None
    return secret, fetched


def set_allowed_models(
    conn: sqlite3.Connection,
    key_id: int,
    models: list[str] | None,
) -> None:
    """Update a key's allowlist. None clears the restriction; [] denies all.

    Returns nothing; callers should check existence separately via get_by_id.
    """
    encoded = json.dumps(list(models)) if models is not None else None
    conn.execute(
        "UPDATE api_keys SET allowed_models=? WHERE id=?",
        (encoded, key_id),
    )


def get_by_id(conn: sqlite3.Connection, key_id: int) -> ApiKey | None:
    row = conn.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
    return _row_to_key(row) if row else None


def verify(conn: sqlite3.Connection, secret: str) -> ApiKey | None:
    """Look up a key by secret; returns None if missing or revoked.

    SELECT and UPDATE run inside a single locked section so the row read
    cannot be invalidated by a concurrent write from another worker thread.

    Security note: there is no post-DB hmac.compare_digest because the SELECT
    already filtered on `key_hash=?` (byte-exact). What we'd be comparing is
    two SHA-256 hex digests known to be equal - not the user-provided secret.
    Constant-time concerns apply to the *secret* string only; here the secret
    has already been hashed to a fixed-length digest before any comparison.
    If the SELECT predicate is ever loosened (LIKE, prefix match, etc.) this
    decision must be revisited.
    """
    candidate_hash = _hash(secret)
    with db.locked(conn):
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL",
            (candidate_hash,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE api_keys SET last_used_at=CURRENT_TIMESTAMP WHERE id=?",
            (row["id"],),
        )
        return _row_to_key(row)


def list_all(conn: sqlite3.Connection) -> list[ApiKey]:
    rows = conn.execute(
        "SELECT * FROM api_keys ORDER BY id"
    ).fetchall()
    return [_row_to_key(r) for r in rows]


def revoke(conn: sqlite3.Connection, key_id: int) -> None:
    conn.execute(
        "UPDATE api_keys SET revoked_at=CURRENT_TIMESTAMP WHERE id=?",
        (key_id,),
    )


def count_active(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM api_keys WHERE revoked_at IS NULL"
    ).fetchone()
    return int(row["n"])
