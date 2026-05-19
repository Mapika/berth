"""Show predictor candidates, tick stats, or replay a trace.

Modes:
- (default)     current top predictor candidates
- --stats       tick-loop counters
- --export PATH dump usage_events to a JSONL file (offline, no daemon needed)
- --replay PATH compare a recorded JSONL trace against an LRU baseline
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import typer

from berth import config
from berth.cli import app, ipc
from berth.lifecycle.replay import ReplayEvent, simulate_lru


def _export_to_jsonl(out_path: Path) -> int:
    """Read usage_events directly from the SQLite file (no daemon round-trip)
    and write one JSON object per line. Returns rows written."""
    if not config.DB_PATH.exists():
        raise typer.BadParameter(f"no usage DB at {config.DB_PATH}")
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT ts, base_name, adapter_name, cold_loaded, api_key_id, "
            "deployment_id FROM usage_events ORDER BY ts ASC"
        )
        n = 0
        with out_path.open("w") as f:
            for row in cursor:
                f.write(json.dumps({
                    "ts": row["ts"],
                    "base": row["base_name"],
                    "adapter": row["adapter_name"],
                    "cold_loaded": bool(row["cold_loaded"]),
                    "api_key_id": row["api_key_id"],
                    "deployment_id": row["deployment_id"],
                }) + "\n")
                n += 1
        return n
    finally:
        conn.close()


def _read_jsonl(path: Path) -> list[ReplayEvent]:
    events: list[ReplayEvent] = []
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise typer.BadParameter(
                    f"{path}:{lineno}: invalid JSON ({e.msg})"
                ) from e
            missing = {"ts", "base", "cold_loaded"} - row.keys()
            if missing:
                raise typer.BadParameter(
                    f"{path}:{lineno}: missing fields {sorted(missing)}"
                )
            events.append(ReplayEvent(
                ts=row["ts"],
                base=row["base"],
                adapter=row.get("adapter"),
                cold_loaded=bool(row["cold_loaded"]),
            ))
    return events


@app.command("predict")
def predict(
    stats: bool = typer.Option(
        False, "--stats",
        help="Show tick-loop counters instead of the candidate list.",
    ),
    export: Path = typer.Option(
        None, "--export",
        help="Dump usage_events to a JSONL file (reads ~/.berth/db.sqlite "
             "directly; no daemon needed).",
    ),
    replay: Path = typer.Option(
        None, "--replay",
        help="Replay a JSONL trace through an LRU baseline and report "
             "cold-load reduction. Offline; no daemon needed.",
    ),
    slots: int = typer.Option(
        4, "--slots",
        help="LRU slots per base for --replay (matches deployment max_loras).",
    ),
):
    """Show the predictor's current top candidates (default), runtime stats
    (--stats), dump events (--export), or compare a recorded trace against
    an LRU baseline (--replay)."""
    modes = sum(1 for x in (stats, export, replay) if x)
    if modes > 1:
        typer.echo("--stats, --export, and --replay are mutually exclusive", err=True)
        raise typer.Exit(2)

    if export is not None:
        n = _export_to_jsonl(export)
        typer.echo(f"exported {n} events to {export}")
        return

    if replay is not None:
        events = _read_jsonl(replay)
        if not events:
            typer.echo(f"{replay}: no events to replay")
            return
        result = simulate_lru(events, slots_per_base=slots)
        if result.total == 0:
            typer.echo(
                f"no adapter events in {replay} (all bare-base); nothing to compare"
            )
            return
        typer.echo(f"events (adapter-bearing):  {result.total}")
        typer.echo(
            f"recorded cold-loads:       {result.recorded_cold} "
            f"({result.recorded_rate * 100:.1f}%)"
        )
        typer.echo(
            f"LRU baseline cold-loads:   {result.lru_cold} "
            f"({result.lru_rate * 100:.1f}%) "
            f"[slots/base={result.slots_per_base}]"
        )
        typer.echo(f"reduction vs LRU:          {result.reduction_pct:.1f}%")
        return

    if stats:
        result = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/predictor/stats"))
        typer.echo(f"enabled:                       {result['enabled']}")
        if result["enabled"]:
            typer.echo(f"tick_interval_s:               {result['tick_interval_s']}")
            typer.echo(f"max_prewarm_per_tick:          {result['max_prewarm_per_tick']}")
            typer.echo(
                f"max_base_prewarm_per_tick:     "
                f"{result.get('max_base_prewarm_per_tick', 0)}"
            )
        typer.echo(f"preloads_attempted:            {result['preloads_attempted']}")
        typer.echo(f"preloads_succeeded:            {result['preloads_succeeded']}")
        typer.echo(f"preloads_skipped_already_warm: {result['preloads_skipped_already_warm']}")
        typer.echo(f"preloads_skipped_no_deployment:{result['preloads_skipped_no_deployment']}")
        typer.echo(
            f"base_prewarms_attempted:       "
            f"{result.get('base_prewarms_attempted', 0)}"
        )
        typer.echo(
            f"base_prewarms_succeeded:       "
            f"{result.get('base_prewarms_succeeded', 0)}"
        )
        typer.echo(
            f"base_prewarms_skipped_no_plan: "
            f"{result.get('base_prewarms_skipped_no_plan', 0)}"
        )
        return

    rows = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/predictor/candidates"))
    if not rows:
        typer.echo("no candidates (predictor has nothing to suggest right now)")
        return
    typer.echo(f"{'MODEL':<36}{'SCORE':<8}REASON")
    for r in rows:
        if r["adapter_name"]:
            model = f"{r['base_name']}:{r['adapter_name']}"
        else:
            model = r["base_name"]
        if len(model) > 35:
            model = model[:32] + "..."
        typer.echo(f"{model:<36}{r['score']:<8.3f}{r['reason']}")
