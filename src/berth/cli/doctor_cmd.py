from __future__ import annotations

import json

import typer

from berth import config as berth_config
from berth.cli import app
from berth.doctor.runner import run_all, summarise

_LABEL = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
_COLOR = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW, "fail": typer.colors.RED}


@app.command("doctor")
def doctor(
    json_out: bool = typer.Option(False, "--json"),
    leader_only: bool | None = typer.Option(
        None, "--leader-only/--no-leader-only",
        help="Skip Docker/GPU/engine-image checks. Defaults to the value "
        "of leader_only in the resolved config.",
    ),
):
    """Diagnose the local environment (Docker, GPUs, paths, ports, images)."""
    if leader_only is None:
        leader_only = berth_config.resolve_config().leader_only
    results = run_all(leader_only=leader_only)
    if json_out:
        typer.echo(json.dumps([{
            "name": r.name, "status": r.status, "detail": r.detail, "fix": r.fix
        } for r in results], indent=2))
        raise typer.Exit(_exit_code(results))
    for r in results:
        label = _LABEL.get(r.status, "?")
        color = _COLOR.get(r.status, typer.colors.WHITE)
        typer.secho(f"  {label:<4} {r.name:<20} {r.detail}", fg=color)
        if r.fix and r.status != "ok":
            typer.echo(f"     -> {r.fix}")
    ok, warn, fail = summarise(results)
    typer.echo()
    typer.secho(
        f"{ok} ok, {warn} warn, {fail} fail",
        fg=(typer.colors.RED if fail else (typer.colors.YELLOW if warn else typer.colors.GREEN)),
    )
    raise typer.Exit(_exit_code(results))


def _exit_code(results) -> int:
    if any(r.status == "fail" for r in results):
        return 1
    return 0
