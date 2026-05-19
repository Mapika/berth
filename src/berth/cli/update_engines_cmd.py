from __future__ import annotations

import typer

from berth.backends.hub import latest_stable_tag
from berth.backends.manifest import load_manifest, write_override
from berth.cli import app


@app.command("update-engines")
def update_engines(
    apply: bool = typer.Option(
        False, "--apply",
        help="Write the new tags to ~/.serve/backends.override.yaml",
    ),
):
    """Check Docker Hub for newer pinned-tag stable releases of each engine."""
    manifest = load_manifest()
    typer.echo(f"{'ENGINE':<10} {'CURRENT':<20} {'LATEST':<20} {'STATUS'}")
    updates: dict[str, dict] = {}
    for name, m in manifest.items():
        latest = latest_stable_tag(m.image)
        if latest is None:
            status = "could not query Docker Hub"
        elif latest == m.pinned_tag:
            status = "up to date"
        else:
            status = "update available"
            updates[name] = {"pinned_tag": latest}
        typer.echo(f"{name:<10} {m.pinned_tag:<20} {(latest or '-'):<20} {status}")

    if not updates:
        typer.echo("\nNo updates available.")
        return

    if not apply:
        typer.echo("\nRun `serve update-engines --apply` to write the new tags.")
        return

    path = write_override(updates)
    typer.echo(f"\nWrote {path}")
    typer.echo("Restart the daemon for changes to take effect: serve daemon restart")
