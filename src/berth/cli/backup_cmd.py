"""`berth backup` — snapshot the daemon's recoverable state to a tarball.

The DR set is: ~/.berth/db.sqlite (taken via SQLite's `.backup` so the
WAL is consistent), ~/.berth/ca/ (CA cert + private key — the
keys-to-the-kingdom for the cluster), ~/.berth/key_pepper (without it
every API key falls), and ~/.berth/config.toml. Model weights and the
logs/ directory are deliberately excluded — model weights are large
and re-downloadable, logs are an operations artefact.
"""
from __future__ import annotations

import os
import sqlite3
import tarfile
import time
from pathlib import Path

import typer

from berth import config
from berth.cli import app

backup_app = typer.Typer(help="Snapshot and restore daemon state.")
app.add_typer(backup_app, name="backup")


@backup_app.command("create")
def create_backup(
    dest: str = typer.Argument(
        ...,
        help="Path to write the tarball, e.g. /var/backups/serve-2026-05-20.tar.gz",
    ),
) -> None:
    """Tarball db.sqlite (consistent .backup snapshot), ca/, key_pepper, config.toml."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Take a hot-snapshot of the sqlite file. Using .backup avoids the
    # well-known WAL-tail truncation bug of a naive `cp db.sqlite`.
    snapshot_path = (
        config.BERTH_DIR / f".db-backup-{int(time.time())}.sqlite"
    )
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(snapshot_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    try:
        fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                fd = -1
                with tarfile.open(fileobj=f, mode="w:gz") as tar:
                    tar.add(str(snapshot_path), arcname="db.sqlite")
                    if (config.BERTH_DIR / "ca").exists():
                        tar.add(
                            str(config.BERTH_DIR / "ca"), arcname="ca",
                        )
                    if (config.BERTH_DIR / "key_pepper").exists():
                        tar.add(
                            str(config.BERTH_DIR / "key_pepper"),
                            arcname="key_pepper",
                        )
                    if config.CONFIG_FILE.exists():
                        tar.add(str(config.CONFIG_FILE), arcname="config.toml")
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        snapshot_path.unlink(missing_ok=True)
    typer.echo(f"wrote backup → {dest_path}")
