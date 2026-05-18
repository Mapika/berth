from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SERVE_DIR = Path(os.environ.get("SERVE_HOME", Path.home() / ".serve"))
MODELS_DIR = SERVE_DIR / "models"
LOGS_DIR = SERVE_DIR / "logs"
CONFIGS_DIR = SERVE_DIR / "configs"  # per-deployment engine YAMLs (TRT-LLM --config)
DB_PATH = SERVE_DIR / "db.sqlite"
SOCK_PATH = SERVE_DIR / "sock"

DEFAULT_PUBLIC_HOST = "127.0.0.1"
DEFAULT_PUBLIC_PORT = 11500
DEFAULT_CLUSTER_PORT = 11501
DEFAULT_BIND = "0.0.0.0"

CONFIG_FILE = SERVE_DIR / "config.toml"
LEADER_DIR = SERVE_DIR / "leader"

DOCKER_NETWORK_NAME = "serve-engines"


# ---------------------------------------------------------------------------
# Public/cluster address resolution
# ---------------------------------------------------------------------------


@dataclass
class ResolvedConfig:
    """Final resolved bind + advertise addresses, plus where each came from.

    `source` maps each field name to one of: "flag", "env", "file",
    "autodetect", "default". Used by `serve config show` to explain
    decisions and by the startup banner to warn on autodetect."""

    public_host: str
    public_port: int
    public_bind: str
    cluster_host: str
    cluster_port: int
    cluster_bind: str
    public_cert_path: Path | None
    public_key_path: Path | None
    leader_url_override: str | None  # SERVE_LEADER_URL
    source: dict[str, str] = field(default_factory=dict)

    @property
    def public_url(self) -> str:
        return f"https://{self.public_host}:{self.public_port}"

    @property
    def cluster_url(self) -> str:
        """The URL we advertise to remote agents (enrollment URIs)."""
        return self.leader_url_override or (
            f"https://{self.cluster_host}:{self.cluster_port}"
        )


def load_config_file() -> dict:
    """Read ~/.serve/config.toml. Returns {} if absent or malformed."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with CONFIG_FILE.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def save_config_file(updates: dict[str, dict[str, str | int]]) -> None:
    """Deep-merge `updates` into ~/.serve/config.toml and write it back.

    `updates` is a section-table mapping like `{"public": {"host": "..."}}`.
    Existing sections/keys are preserved; new keys overwrite. Setting a
    value to None removes the key from its section."""
    current = load_config_file()
    for section, kvs in updates.items():
        sec = dict(current.get(section, {}))
        for k, v in kvs.items():
            if v is None:
                sec.pop(k, None)
            else:
                sec[k] = v
        if sec:
            current[section] = sec
        else:
            current.pop(section, None)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(_dump_toml(current))


def _dump_toml(data: dict) -> str:
    """Tiny TOML writer covering only the subset we use (sections of
    flat str/int values). Avoids adding tomli-w as a dependency."""
    lines: list[str] = []
    for section, kvs in data.items():
        lines.append(f"[{section}]")
        for k, v in kvs.items():
            if isinstance(v, bool):
                lines.append(f'{k} = {"true" if v else "false"}')
            elif isinstance(v, int):
                lines.append(f"{k} = {v}")
            else:
                escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k} = "{escaped}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def autodetect_outbound_ip() -> str | None:
    """Return the IPv4 of the interface used for outbound traffic.

    Uses a UDP-connect trick: a SOCK_DGRAM connect to a routable address
    causes the kernel to pick the source IP without sending packets.
    Falls back to gethostbyname(gethostname()). Returns None if both fail
    or only loopback is available."""
    candidates: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            candidates.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        candidates.append(ip)
    except OSError:
        pass
    for ip in candidates:
        if ip and not ip.startswith("127."):
            return ip
    return candidates[0] if candidates else None


def resolve_config(
    *,
    cli_public_host: str | None = None,
    cli_public_port: int | None = None,
    cli_public_bind: str | None = None,
    cli_cluster_host: str | None = None,
    cli_cluster_port: int | None = None,
    cli_cluster_bind: str | None = None,
    cli_public_cert: str | None = None,
    cli_public_key: str | None = None,
    env: dict[str, str] | None = None,
) -> ResolvedConfig:
    """Resolve effective config from flags → env → file → autodetect/default.

    Pass `env` to override `os.environ` (used by tests). `cli_*` are
    already-parsed CLI overrides (None = not set)."""
    env = env if env is not None else os.environ  # type: ignore[assignment]
    file_cfg = load_config_file()
    public_file = file_cfg.get("public", {})
    cluster_file = file_cfg.get("cluster", {})
    tls_file = file_cfg.get("public_tls", {})
    source: dict[str, str] = {}

    def _pick(
        field_name: str,
        cli_val: str | int | None,
        env_key: str | None,
        file_section: dict,
        file_key: str,
        default,
        autodetect=None,
    ):
        if cli_val is not None:
            source[field_name] = "flag"
            return cli_val
        if env_key and env.get(env_key):
            source[field_name] = "env"
            v = env[env_key]
            return int(v) if isinstance(default, int) else v
        if file_key in file_section:
            source[field_name] = "file"
            return file_section[file_key]
        if autodetect is not None:
            detected = autodetect()
            if detected:
                source[field_name] = "autodetect"
                return detected
        source[field_name] = "default"
        return default

    public_host = _pick(
        "public_host", cli_public_host, "SERVE_PUBLIC_HOST",
        public_file, "host", DEFAULT_PUBLIC_HOST,
        autodetect=autodetect_outbound_ip,
    )
    public_port = _pick(
        "public_port", cli_public_port, "SERVE_PUBLIC_PORT",
        public_file, "port", DEFAULT_PUBLIC_PORT,
    )
    public_bind = _pick(
        "public_bind", cli_public_bind, "SERVE_PUBLIC_BIND",
        public_file, "bind", DEFAULT_BIND,
    )
    cluster_host = _pick(
        "cluster_host", cli_cluster_host, "SERVE_CLUSTER_HOST",
        cluster_file, "host", public_host,  # default to public_host
    )
    if source["cluster_host"] == "default":
        # Re-tag: we inherited from public_host, not a literal default
        source["cluster_host"] = (
            f"inherit:public_host({source['public_host']})"
        )
    cluster_port = _pick(
        "cluster_port", cli_cluster_port, "SERVE_CLUSTER_PORT",
        cluster_file, "port", DEFAULT_CLUSTER_PORT,
    )
    cluster_bind = _pick(
        "cluster_bind", cli_cluster_bind, "SERVE_CLUSTER_BIND",
        cluster_file, "bind", DEFAULT_BIND,
    )
    public_cert = _pick(
        "public_cert_path", cli_public_cert, "SERVE_PUBLIC_CERT",
        tls_file, "cert", None,
    )
    public_key = _pick(
        "public_key_path", cli_public_key, "SERVE_PUBLIC_KEY",
        tls_file, "key", None,
    )
    leader_override = env.get("SERVE_LEADER_URL")
    if leader_override:
        source["leader_url"] = "env:SERVE_LEADER_URL"

    return ResolvedConfig(
        public_host=str(public_host),
        public_port=int(public_port),
        public_bind=str(public_bind),
        cluster_host=str(cluster_host),
        cluster_port=int(cluster_port),
        cluster_bind=str(cluster_bind),
        public_cert_path=Path(public_cert) if public_cert else None,
        public_key_path=Path(public_key) if public_key else None,
        leader_url_override=leader_override,
        source=source,
    )
