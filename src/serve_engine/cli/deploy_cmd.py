"""`serve deploy` — one-shot provisioning helpers for a fresh VPS.

`serve deploy bootstrap` brings the daemon up to a state where:
- ~/.serve/config.toml is configured for behind-Caddy mode (or direct
  TLS, depending on flags),
- the sqlite DB is initialised (so migrations run once, here, before
  the daemon ever takes traffic),
- the key pepper exists at ~/.serve/key_pepper,
- a first admin API key is minted and printed once,
- a ready-to-paste Caddyfile snippet is printed,
- and the operator gets clear next-step instructions for systemd.

The command deliberately does NOT install Caddy, docker, or systemd
units — those are sudo-territory and OS-specific. We print exactly
what the operator needs to do next and let them run those commands
themselves so nothing surprising happens.
"""
from __future__ import annotations

import importlib.resources
import re
from pathlib import Path

import typer

from serve_engine import config
from serve_engine.cli import app

deploy_app = typer.Typer(help="Bootstrap and operational helpers.")
app.add_typer(deploy_app, name="deploy")


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*(?<!-)$"
)


def _ok_hostname(s: str) -> bool:
    return bool(_HOSTNAME_RE.match(s))


def _caddyfile(domain: str, port: int) -> str:
    return f"""# /etc/caddy/Caddyfile — drop this in and `sudo systemctl reload caddy`
{domain} {{
    reverse_proxy 127.0.0.1:{port} {{
        header_up X-Forwarded-For {{remote_host}}
        header_up X-Forwarded-Proto https
    }}
}}
"""


def _systemd_snippet() -> str:
    """Return the in-package systemd unit content for printing /
    writing alongside the bootstrap output. Falls back to a short
    pointer if the file isn't shipped (dev install)."""
    try:
        pkg = importlib.resources.files("serve_engine")
        # The unit ships under packaging/ at repo root, not under the
        # installed package. Try to find it via the source path; on a
        # wheel install operators get the pointer message.
        candidate = Path(str(pkg)).parent.parent / "packaging" / "serve-engine.service"
        if candidate.exists():
            return candidate.read_text()
    except Exception:
        pass
    return ""


def _bootstrap(
    *,
    domain: str,
    public_port: int,
    cluster_port: int,
    behind_proxy: bool,
    serve_home: Path,
    force: bool,
) -> dict[str, str]:
    """Pure-ish workhorse: writes config + DB state, returns a dict of
    artefacts the CLI prints. Separated from the typer entry point so
    tests can drive it without invoking the CLI runner."""
    serve_home.mkdir(parents=True, exist_ok=True)
    # Point all our config constants at this serve_home — important when
    # operators set SERVE_HOME or pass --serve-home.
    config.SERVE_DIR = serve_home  # type: ignore[misc]
    config.MODELS_DIR = serve_home / "models"
    config.LOGS_DIR = serve_home / "logs"
    config.CONFIGS_DIR = serve_home / "configs"
    config.DB_PATH = serve_home / "db.sqlite"
    config.SOCK_PATH = serve_home / "sock"
    config.CONFIG_FILE = serve_home / "config.toml"
    config.LEADER_DIR = serve_home / "leader"

    out: dict[str, str] = {}

    if config.CONFIG_FILE.exists() and not force:
        out["config_status"] = "exists; not overwritten (pass --force to replace)"
    else:
        public_section: dict[str, str | int | bool | None] = {
            "host": domain,
            "port": public_port,
            "bind": "127.0.0.1" if behind_proxy else "0.0.0.0",
        }
        if behind_proxy:
            public_section["scheme"] = "http"
            public_section["trust_proxy_headers"] = True
            public_section["forwarded_allow_ips"] = "127.0.0.1"
        cluster_section: dict[str, str | int | bool | None] = {
            "host": domain,
            "port": cluster_port,
            "bind": "0.0.0.0",
        }
        # save_config_file expects {section: {k: v}}; we hand-roll the
        # bool value because save_config_file's writer handles bools.
        config.save_config_file({
            "public": public_section,
            "cluster": cluster_section,
        })
        out["config_status"] = f"wrote {config.CONFIG_FILE}"

    # Init the DB and CA + pepper. Doing it here (rather than at first
    # daemon start) means migrations run under the file lock with a known
    # operator at the keyboard, and the pepper exists before any key is
    # minted.
    from serve_engine.cluster.ca import generate_ca
    from serve_engine.store import api_keys, db

    ca_dir = serve_home / "ca"
    if not (ca_dir / "ca.crt").exists():
        generate_ca(ca_dir, common_name="serve-engine-ca")
        out["ca_status"] = f"generated {ca_dir}/ca.crt + ca.key (mode 0600)"
    else:
        out["ca_status"] = f"{ca_dir} already provisioned"

    api_keys.configure_pepper(serve_home / "key_pepper")

    conn = db.connect(config.DB_PATH)
    db.init_schema(conn)
    out["db_status"] = f"migrations applied to {config.DB_PATH}"

    if api_keys.count_active(conn) == 0:
        secret, key = api_keys.create(
            conn, name="root", tier="admin",
        )
        out["first_key"] = secret
        out["first_key_id"] = str(key.id)
    else:
        out["first_key"] = ""
        out["first_key_id"] = ""

    out["caddyfile"] = _caddyfile(domain, public_port)
    out["systemd_unit"] = _systemd_snippet()
    return out


@deploy_app.command("bootstrap")
def bootstrap(
    domain: str = typer.Option(
        ..., "--domain",
        help="Public FQDN clients use to reach the leader, e.g. serve.example.com",
    ),
    public_port: int = typer.Option(
        11500, "--public-port",
        help="Loopback port the daemon binds for Caddy to proxy to.",
    ),
    cluster_port: int = typer.Option(
        11501, "--cluster-port",
        help="External-facing port for the mTLS cluster listener.",
    ),
    direct_tls: bool = typer.Option(
        False, "--direct-tls",
        help="Bind the public listener directly with TLS instead of "
        "running behind Caddy. Mostly useful when you already manage "
        "certs through another mechanism.",
    ),
    serve_home: str = typer.Option(
        None, "--serve-home",
        help="Override the daemon's home directory (default: ~/.serve).",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Overwrite an existing config.toml.",
    ),
):
    """Provision a fresh VPS to a ready-to-start leader configuration.

    Writes config.toml, initialises the DB and CA, mints the first
    admin key, and prints a Caddyfile + next-step instructions.
    Idempotent except for first-key minting (only happens when the
    keys table is empty)."""
    if not _ok_hostname(domain):
        raise typer.BadParameter(
            f"--domain {domain!r} doesn't look like a hostname",
        )
    home = Path(serve_home).expanduser() if serve_home else config.SERVE_DIR

    out = _bootstrap(
        domain=domain,
        public_port=public_port,
        cluster_port=cluster_port,
        behind_proxy=not direct_tls,
        serve_home=home,
        force=force,
    )

    typer.echo("=" * 70)
    typer.echo(f"serve-engine bootstrap → {home}")
    typer.echo("=" * 70)
    typer.echo(f"  config : {out['config_status']}")
    typer.echo(f"  ca     : {out['ca_status']}")
    typer.echo(f"  db     : {out['db_status']}")
    typer.echo("")

    if out["first_key"]:
        typer.echo("First admin key (shown once; save it now):")
        typer.echo("")
        typer.echo(f"    {out['first_key']}")
        typer.echo("")
    else:
        typer.echo("Keys already exist; skipped first-key mint.")
        typer.echo("")

    typer.echo("─" * 70)
    typer.echo("Next steps:")
    typer.echo("─" * 70)
    if not direct_tls:
        typer.echo("1. Install the Caddyfile below at /etc/caddy/Caddyfile, then")
        typer.echo("     sudo systemctl reload caddy")
        typer.echo("")
        typer.echo(out["caddyfile"])
    typer.echo("2. Install + start the systemd unit:")
    typer.echo("     sudo cp packaging/serve-engine.service /etc/systemd/system/")
    typer.echo("     sudo systemctl daemon-reload")
    typer.echo("     sudo systemctl enable --now serve-engine")
    typer.echo("")
    typer.echo("3. Verify:")
    typer.echo(f"     curl https://{domain}/healthz")
    typer.echo(f"     curl -H 'Authorization: Bearer <KEY>' https://{domain}/metrics")
    typer.echo("")
    typer.echo("4. Enroll an agent on a GPU host:")
    typer.echo("     serve nodes enroll <label>            # on this leader")
    typer.echo("     serve agent register --uri '<paste>'  # on the agent host")
    typer.echo("     serve agent start")
    typer.echo("")
    typer.echo("Back up the DR set (db + ca + key_pepper + config) regularly:")
    typer.echo("     serve backup create /var/backups/serve-$(date +%F).tar.gz")
