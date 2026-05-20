"""`berth deploy` — one-shot provisioning helpers for a fresh VPS.

`berth deploy bootstrap` brings the daemon up to a state where:
- ~/.berth/config.toml is configured for behind-Caddy mode (or direct
  TLS, depending on flags),
- the sqlite DB is initialised (so migrations run once, here, before
  the daemon ever takes traffic),
- the key pepper exists at ~/.berth/key_pepper,
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

from berth import config
from berth.cli import app

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
    header {{
        Strict-Transport-Security "max-age=31536000"
    }}
    reverse_proxy 127.0.0.1:{port} {{
        header_up X-Forwarded-Proto https
    }}
}}
"""


def _caddyfile_sni_443(
    domain: str,
    public_port: int,
    *,
    tls_port: int = 8443,
    cluster_domain: str | None = None,
) -> str:
    cluster_http = (
        f"""
http://{cluster_domain} {{
    respond 404
}}
"""
        if cluster_domain and cluster_domain != domain
        else ""
    )
    return f"""# /etc/caddy/Caddyfile — Caddy serves public HTTPS on loopback.
# HAProxy owns :443 and forwards SNI {domain!r} to 127.0.0.1:{tls_port}.
{{
    auto_https disable_redirects
}}

http://{domain} {{
    redir https://{domain}{{uri}} permanent
}}
{cluster_http}

https://{domain}:{tls_port} {{
    bind 127.0.0.1
    header {{
        # Caddy is behind HAProxy on :443; do not advertise loopback :{tls_port}
        # as an external HTTP/3 endpoint.
        -Alt-Svc
        Strict-Transport-Security "max-age=31536000"
    }}
    reverse_proxy 127.0.0.1:{public_port} {{
        header_up X-Forwarded-Proto https
    }}
}}
"""


def _haproxy_sni_443(
    *,
    public_domain: str,
    cluster_domain: str,
    public_tls_port: int = 8443,
    cluster_port: int = 11501,
) -> str:
    return f"""# /etc/haproxy/haproxy.cfg — TLS passthrough SNI router for berth.
# {public_domain}:443  -> Caddy public HTTPS on 127.0.0.1:{public_tls_port}
# {cluster_domain}:443 -> berth cluster mTLS on 127.0.0.1:{cluster_port}
global
    log /dev/log local0
    log /dev/log local1 notice
    chroot /var/lib/haproxy
    stats socket /run/haproxy/admin.sock mode 660 level admin
    stats timeout 30s
    user haproxy
    group haproxy
    daemon

defaults
    log global
    mode tcp
    option tcplog
    timeout connect 5s
    timeout client  1h
    timeout server  1h

frontend berth_https
    bind *:443
    tcp-request inspect-delay 5s
    tcp-request content accept if {{ req.ssl_hello_type 1 }}
    use_backend berth_cluster if {{ req.ssl_sni -i {cluster_domain} }}
    use_backend berth_public if {{ req.ssl_sni -i {public_domain} }}
    default_backend berth_public

backend berth_public
    server caddy_public 127.0.0.1:{public_tls_port} check

backend berth_cluster
    server berth_cluster 127.0.0.1:{cluster_port} check
"""


def _systemd_snippet(*, leader_url: str | None = None) -> str:
    """Return the in-package systemd unit content for printing /
    writing alongside the bootstrap output. Falls back to a short
    pointer if the file isn't shipped (dev install)."""
    try:
        pkg = importlib.resources.files("berth")
        # The unit ships under packaging/ at repo root, not under the
        # installed package. Try to find it via the source path; on a
        # wheel install operators get the pointer message.
        candidate = Path(str(pkg)).parent.parent / "packaging" / "berth.service"
        if candidate.exists():
            text = candidate.read_text()
            if leader_url:
                text = text.replace(
                    "Environment=BERTH_HOME=/var/lib/berth\n",
                    "Environment=BERTH_HOME=/var/lib/berth\n"
                    f"Environment=BERTH_LEADER_URL={leader_url}\n",
                )
            return text
    except Exception:
        pass  # nosec
    return ""


def _bootstrap(
    *,
    domain: str,
    cluster_domain: str | None = None,
    public_port: int,
    cluster_port: int,
    public_tls_port: int = 8443,
    behind_proxy: bool,
    sni_443: bool = False,
    leader_only: bool = False,
    serve_home: Path,
    force: bool,
) -> dict[str, str]:
    """Pure-ish workhorse: writes config + DB state, returns a dict of
    artefacts the CLI prints. Separated from the typer entry point so
    tests can drive it without invoking the CLI runner."""
    config.ensure_private_dir(serve_home)
    # Point all our config constants at this serve_home — important when
    # operators set SERVE_HOME or pass --serve-home.
    config.BERTH_DIR = serve_home  # type: ignore[misc]
    config.MODELS_DIR = serve_home / "models"
    config.LOGS_DIR = serve_home / "logs"
    config.CONFIGS_DIR = serve_home / "configs"
    config.DB_PATH = serve_home / "db.sqlite"
    config.SOCK_PATH = serve_home / "sock"
    config.CONFIG_FILE = serve_home / "config.toml"
    config.LEADER_DIR = serve_home / "leader"

    out: dict[str, str] = {}
    cluster_host = cluster_domain or domain
    advertised_leader_url = (
        f"https://{cluster_host}" if sni_443 else f"https://{cluster_host}:{cluster_port}"
    )

    if config.CONFIG_FILE.exists() and not force:
        out["config_status"] = "exists; not overwritten (pass --force to replace)"
    else:
        public_section: dict[str, str | int | bool | None] = {
            "host": domain,
            "port": public_port,
            "bind": "127.0.0.1" if behind_proxy else "0.0.0.0",  # nosec
        }
        if behind_proxy:
            public_section["scheme"] = "http"
            public_section["trust_proxy_headers"] = True
            public_section["forwarded_allow_ips"] = "127.0.0.1"
        cluster_section: dict[str, str | int | bool | None] = {
            "host": cluster_host,
            "port": cluster_port,
            "bind": "127.0.0.1" if sni_443 else "0.0.0.0",  # nosec
        }
        # save_config_file expects {section: {k: v}}; we hand-roll the
        # bool value because save_config_file's writer handles bools.
        updates: dict[str, dict[str, str | int | bool | None]] = {
            "public": public_section,
            "cluster": cluster_section,
        }
        if leader_only:
            updates["server"] = {"leader_only": True}
        config.save_config_file(updates)
        out["config_status"] = f"wrote {config.CONFIG_FILE}"

    # Init the DB and CA + pepper. Doing it here (rather than at first
    # daemon start) means migrations run under the file lock with a known
    # operator at the keyboard, and the pepper exists before any key is
    # minted.
    from berth.cluster.ca import generate_ca
    from berth.store import api_keys, db

    ca_dir = serve_home / "ca"
    if not (ca_dir / "ca.crt").exists():
        generate_ca(ca_dir, common_name="berth-ca")
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

    out["leader_url"] = advertised_leader_url
    out["caddyfile"] = (
        _caddyfile_sni_443(
            domain,
            public_port,
            tls_port=public_tls_port,
            cluster_domain=cluster_host,
        )
        if sni_443
        else _caddyfile(domain, public_port)
    )
    out["haproxy"] = (
        _haproxy_sni_443(
            public_domain=domain,
            cluster_domain=cluster_host,
            public_tls_port=public_tls_port,
            cluster_port=cluster_port,
        )
        if sni_443
        else ""
    )
    out["systemd_unit"] = _systemd_snippet(
        leader_url=advertised_leader_url if sni_443 else None,
    )
    return out


@deploy_app.command("bootstrap")
def bootstrap(
    domain: str = typer.Option(
        ..., "--domain",
        help="Public FQDN clients use to reach the leader, e.g. serve.example.com",
    ),
    cluster_domain: str = typer.Option(
        None, "--cluster-domain",
        help="Cluster FQDN agents use. Required with --sni-443, "
        "e.g. cluster.example.com.",
    ),
    public_port: int = typer.Option(
        11500, "--public-port",
        help="Loopback port the daemon binds for Caddy to proxy to.",
    ),
    cluster_port: int = typer.Option(
        11501, "--cluster-port",
        help="External-facing port for the mTLS cluster listener.",
    ),
    public_tls_port: int = typer.Option(
        8443, "--public-tls-port",
        help="Loopback TLS port Caddy uses behind HAProxy when --sni-443 is set.",
    ),
    sni_443: bool = typer.Option(
        False, "--sni-443",
        help="Generate HAProxy+Caddy config so public and cluster hostnames "
        "share external 443 by SNI. Keeps the berth cluster listener on "
        "loopback with TLS passthrough.",
    ),
    leader_only: bool = typer.Option(
        False, "--leader-only",
        help="Write [server] leader_only = true for a control-plane-only VPS.",
    ),
    direct_tls: bool = typer.Option(
        False, "--direct-tls",
        help="Bind the public listener directly with TLS instead of "
        "running behind Caddy. Mostly useful when you already manage "
        "certs through another mechanism.",
    ),
    serve_home: str = typer.Option(
        None, "--serve-home",
        help="Override the daemon's home directory (default: ~/.berth).",
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
    if cluster_domain and not _ok_hostname(cluster_domain):
        raise typer.BadParameter(
            f"--cluster-domain {cluster_domain!r} doesn't look like a hostname",
        )
    if sni_443 and not cluster_domain:
        raise typer.BadParameter("--sni-443 requires --cluster-domain")
    if sni_443 and direct_tls:
        raise typer.BadParameter("--sni-443 cannot be combined with --direct-tls")
    home = Path(serve_home).expanduser() if serve_home else config.BERTH_DIR

    out = _bootstrap(
        domain=domain,
        cluster_domain=cluster_domain,
        public_port=public_port,
        cluster_port=cluster_port,
        public_tls_port=public_tls_port,
        behind_proxy=not direct_tls,
        sni_443=sni_443,
        leader_only=leader_only,
        serve_home=home,
        force=force,
    )

    typer.echo("=" * 70)
    typer.echo(f"berth bootstrap → {home}")
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
    if sni_443:
        typer.echo("1. Install the Caddyfile below at /etc/caddy/Caddyfile:")
        typer.echo("")
        typer.echo(out["caddyfile"])
        typer.echo("2. Install the HAProxy config below at /etc/haproxy/haproxy.cfg:")
        typer.echo("")
        typer.echo(out["haproxy"])
        typer.echo("3. Reload services:")
        typer.echo("     sudo systemctl reload caddy")
        typer.echo("     sudo systemctl restart haproxy")
        step = 4
    elif not direct_tls:
        typer.echo("1. Install the Caddyfile below at /etc/caddy/Caddyfile, then")
        typer.echo("     sudo systemctl reload caddy")
        typer.echo("")
        typer.echo(out["caddyfile"])
        step = 2
    else:
        step = 1
    typer.echo(f"{step}. Install + start the systemd unit:")
    typer.echo("     sudo cp packaging/berth.service /etc/systemd/system/")
    if sni_443:
        typer.echo(
            f"     sudo systemctl edit berth  # add BERTH_LEADER_URL={out['leader_url']}"
        )
    typer.echo("     sudo systemctl daemon-reload")
    typer.echo("     sudo systemctl enable --now berth")
    typer.echo("")
    typer.echo(f"{step + 1}. Verify:")
    typer.echo(f"     curl https://{domain}/healthz")
    if sni_443 and cluster_domain:
        typer.echo(f"     curl -k https://{cluster_domain}/admin/ca.pem")
    else:
        typer.echo(f"     curl -k https://{domain}:{cluster_port}/admin/ca.pem")
    typer.echo(f"     curl -H 'Authorization: Bearer <KEY>' https://{domain}/metrics")
    typer.echo("")
    typer.echo(f"{step + 2}. Enroll an agent on a GPU host:")
    typer.echo("     berth nodes enroll <label>            # on this leader")
    typer.echo("     berth agent register --uri '<paste>'  # on the agent host")
    typer.echo("     berth agent start")
    typer.echo("")
    typer.echo("Back up the DR set (db + ca + key_pepper + config) regularly:")
    typer.echo("     berth backup create /var/backups/serve-$(date +%F).tar.gz")
