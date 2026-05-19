from __future__ import annotations

from pathlib import Path

import typer

from berth import config
from berth.cli import app

config_app = typer.Typer(
    help="Read and modify ~/.serve/config.toml — public/cluster addresses and TLS."
)
app.add_typer(config_app, name="config")


def _parse_kv(args: list[str]) -> dict[str, str]:
    """Parse `k=v` token list into a dict, validating shape."""
    out: dict[str, str] = {}
    for arg in args:
        if "=" not in arg:
            raise typer.BadParameter(f"expected key=value, got {arg!r}")
        k, v = arg.split("=", 1)
        if not k:
            raise typer.BadParameter(f"empty key in {arg!r}")
        out[k.strip()] = v.strip()
    return out


def _set_section(section: str, allowed: set[str], kvs: dict[str, str]) -> None:
    extra = set(kvs) - allowed
    if extra:
        raise typer.BadParameter(
            f"unknown key(s) for [{section}]: {sorted(extra)}. allowed: {sorted(allowed)}"
        )
    updates: dict[str, str | int | None] = {}
    for k, v in kvs.items():
        if k == "port":
            try:
                updates[k] = int(v)
            except ValueError as e:
                raise typer.BadParameter(
                    f"port must be an integer, got {v!r}"
                ) from e
        else:
            updates[k] = v
    config.save_config_file({section: updates})
    typer.echo(f"updated [{section}] in {config.CONFIG_FILE}")


@config_app.command("set-public")
def set_public(kv: list[str] = typer.Argument(...)):
    """Set [public] keys. Allowed: host, port, bind.

    Example: serve config set-public host=api.example.com port=11500
    """
    _set_section("public", {"host", "port", "bind"}, _parse_kv(kv))


@config_app.command("set-cluster")
def set_cluster(kv: list[str] = typer.Argument(...)):
    """Set [cluster] keys. Allowed: host, port, bind.

    Example: serve config set-cluster host=cluster.example.com bind=10.0.0.1
    """
    _set_section("cluster", {"host", "port", "bind"}, _parse_kv(kv))


@config_app.command("set-public-tls")
def set_public_tls(kv: list[str] = typer.Argument(...)):
    """Set [public_tls] keys. Allowed: cert, key.

    Example: serve config set-public-tls cert=/etc/le/fullchain.pem key=/etc/le/privkey.pem
    """
    kvs = _parse_kv(kv)
    for k in ("cert", "key"):
        if k in kvs and not Path(kvs[k]).is_file():
            typer.echo(f"warning: {k} path does not exist: {kvs[k]}", err=True)
    _set_section("public_tls", {"cert", "key"}, kvs)


@config_app.command("show")
def show():
    """Print the resolved config with each value's source."""
    cfg = config.resolve_config()
    rows = [
        ("public.host", cfg.public_host, cfg.source.get("public_host", "?")),
        ("public.port", cfg.public_port, cfg.source.get("public_port", "?")),
        ("public.bind", cfg.public_bind, cfg.source.get("public_bind", "?")),
        (
            "public_tls.cert", cfg.public_cert_path or "-",
            cfg.source.get("public_cert_path", "?"),
        ),
        (
            "public_tls.key", cfg.public_key_path or "-",
            cfg.source.get("public_key_path", "?"),
        ),
        ("cluster.host", cfg.cluster_host, cfg.source.get("cluster_host", "?")),
        ("cluster.port", cfg.cluster_port, cfg.source.get("cluster_port", "?")),
        ("cluster.bind", cfg.cluster_bind, cfg.source.get("cluster_bind", "?")),
        ("leader_url_override", cfg.leader_url_override or "-",
         cfg.source.get("leader_url", "default")),
    ]
    width = max(len(r[0]) for r in rows)
    for name, val, src in rows:
        typer.echo(f"{name:<{width}}  {val!s:<32}  ({src})")
    typer.echo("")
    typer.echo(f"resolved public_url : {cfg.public_url}")
    typer.echo(f"resolved cluster_url: {cfg.cluster_url}")
    typer.echo(f"config file         : {config.CONFIG_FILE}")
