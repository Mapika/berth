from __future__ import annotations

import os
import socket
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env_get(env_map: Mapping[str, str], key: str) -> str | None:
    """Look up a current berth environment variable."""
    if not key.startswith("BERTH_"):
        raise ValueError(f"expected BERTH_* key, got {key!r}")
    return env_map.get(key)


BERTH_DIR = Path(_env_get(os.environ, "BERTH_HOME") or Path.home() / ".berth")
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MODELS_DIR = BERTH_DIR / "models"
LOGS_DIR = BERTH_DIR / "logs"
CONFIGS_DIR = BERTH_DIR / "configs"  # per-deployment engine YAMLs (TRT-LLM --config)
DB_PATH = BERTH_DIR / "db.sqlite"
SOCK_PATH = BERTH_DIR / "sock"

DEFAULT_PUBLIC_HOST = "127.0.0.1"
DEFAULT_PUBLIC_PORT = 11500
DEFAULT_CLUSTER_PORT = 11501
DEFAULT_BIND = "127.0.0.1"

CONFIG_FILE = BERTH_DIR / "config.toml"
LEADER_DIR = BERTH_DIR / "leader"

DOCKER_NETWORK_NAME = "berth-engines"


# ---------------------------------------------------------------------------
# Public/cluster address resolution
# ---------------------------------------------------------------------------


@dataclass
class ResolvedConfig:
    """Final resolved bind + advertise addresses, plus where each came from.

    `source` maps each field name to one of: "flag", "env", "file",
    "autodetect", "default". Used by `berth config show` to explain
    decisions and by the startup banner to warn on autodetect."""

    public_host: str
    public_port: int
    public_bind: str
    cluster_host: str
    cluster_port: int
    cluster_bind: str
    public_cert_path: Path | None
    public_key_path: Path | None
    public_scheme: str = "https"  # "http" when behind a TLS-terminating proxy
    trust_proxy_headers: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    leader_url_override: str | None = None
    # Control-plane only mode. When True the daemon does not require local
    # Docker or NVIDIA: it serves /v1/*, /admin/*, /metrics, the UI, and the
    # cluster listener, but all deployments must target a remote agent
    # (`--node <label>` or service-profile node_label). Useful for hosting
    # the leader on a small VPS while GPU work runs on enrolled agents.
    leader_only: bool = False
    # Security hardening for internet-facing leaders. When False, admin
    # deploy requests may only use known backend default images and cannot
    # pass raw engine flags that could enable remote-code execution or widen
    # container capabilities. Operators can opt in for lab environments via
    # [server].allow_unsafe_deploy_options=true.
    allow_unsafe_deploy_options: bool = False
    source: dict[str, str] = field(default_factory=dict)

    @property
    def public_url(self) -> str:
        return f"{self.public_scheme}://{self.public_host}:{self.public_port}"

    @property
    def cluster_url(self) -> str:
        """The URL we advertise to remote agents (enrollment URIs)."""
        return self.leader_url_override or (
            f"https://{self.cluster_host}:{self.cluster_port}"
        )


def load_config_file() -> dict[str, Any]:
    """Read ~/.berth/config.toml. Returns {} if absent or malformed."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with CONFIG_FILE.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def ensure_private_dir(path: Path) -> None:
    """Create `path` as an owner-only directory and tighten existing modes."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)


def write_private_file(
    path: Path,
    data: bytes,
    *,
    mode: int = PRIVATE_FILE_MODE,
) -> None:
    """Write secret bytes without a write-then-chmod exposure window."""
    path = Path(path)
    # Tighten parent dirs to 0700 (matching the file's 0600). A bare
    # ``mkdir(parents=True)`` would use the process umask — typically 0022 —
    # leaving an intermediate ``leader/`` or ``keys/`` dir world-traversable
    # if BERTH_HOME ever lives somewhere shared.
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    finally:
        if fd != -1:
            os.close(fd)


def save_config_file(updates: dict[str, dict[str, str | int | bool | None]]) -> None:
    """Deep-merge `updates` into ~/.berth/config.toml and write it back.

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
    ensure_private_dir(CONFIG_FILE.parent)
    CONFIG_FILE.write_text(_dump_toml(current))


def _dump_toml(data: dict[str, Any]) -> str:
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
    cli_leader_only: bool | None = None,
    cli_allow_unsafe_deploy_options: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """Resolve effective config from flags → env → file → autodetect/default.

    Pass `env` to override `os.environ` (used by tests). `cli_*` are
    already-parsed CLI overrides (None = not set)."""
    env_map: Mapping[str, str] = os.environ if env is None else env
    file_cfg = load_config_file()
    public_file = file_cfg.get("public", {})
    cluster_file = file_cfg.get("cluster", {})
    tls_file = file_cfg.get("public_tls", {})
    server_file = file_cfg.get("server", {})
    source: dict[str, str] = {}

    def _pick(
        field_name: str,
        cli_val: str | int | bool | None,
        env_key: str | None,
        file_section: Mapping[str, Any],
        file_key: str,
        default: object,
        autodetect: Any = None,
    ) -> object:
        if cli_val is not None:
            source[field_name] = "flag"
            return cli_val
        if env_key is not None:
            v = _env_get(env_map, env_key)
            if v:
                source[field_name] = "env"
                # bool first — bool is a subclass of int in Python; int() of
                # "true" would otherwise raise.
                if isinstance(default, bool):
                    return v
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
        "public_host", cli_public_host, "BERTH_PUBLIC_HOST",
        public_file, "host", DEFAULT_PUBLIC_HOST,
        autodetect=autodetect_outbound_ip,
    )
    public_port = _pick(
        "public_port", cli_public_port, "BERTH_PUBLIC_PORT",
        public_file, "port", DEFAULT_PUBLIC_PORT,
    )
    public_bind = _pick(
        "public_bind", cli_public_bind, "BERTH_PUBLIC_BIND",
        public_file, "bind", DEFAULT_BIND,
    )
    cluster_host = _pick(
        "cluster_host", cli_cluster_host, "BERTH_CLUSTER_HOST",
        cluster_file, "host", public_host,  # default to public_host
    )
    if source["cluster_host"] == "default":
        # Re-tag: we inherited from public_host, not a literal default
        source["cluster_host"] = (
            f"inherit:public_host({source['public_host']})"
        )
    cluster_port = _pick(
        "cluster_port", cli_cluster_port, "BERTH_CLUSTER_PORT",
        cluster_file, "port", DEFAULT_CLUSTER_PORT,
    )
    cluster_bind = _pick(
        "cluster_bind", cli_cluster_bind, "BERTH_CLUSTER_BIND",
        cluster_file, "bind", DEFAULT_BIND,
    )
    public_cert = _pick(
        "public_cert_path", cli_public_cert, "BERTH_PUBLIC_CERT",
        tls_file, "cert", None,
    )
    public_key = _pick(
        "public_key_path", cli_public_key, "BERTH_PUBLIC_KEY",
        tls_file, "key", None,
    )
    # Reverse-proxy mode. When set, the daemon binds plain HTTP on the
    # public listener (Caddy/Nginx terminates TLS upstream) and trusts
    # X-Forwarded-* headers from the configured proxy IPs.
    public_scheme = _pick(
        "public_scheme", None, "BERTH_PUBLIC_SCHEME",
        public_file, "scheme", "https",
    )
    trust_proxy_headers_raw = _pick(
        "trust_proxy_headers", None, "BERTH_TRUST_PROXY_HEADERS",
        public_file, "trust_proxy_headers", False,
    )
    trust_proxy_headers = (
        str(trust_proxy_headers_raw).lower() in {"1", "true", "yes"}
        if not isinstance(trust_proxy_headers_raw, bool)
        else trust_proxy_headers_raw
    )
    forwarded_allow_ips = _pick(
        "forwarded_allow_ips", None, "BERTH_FORWARDED_ALLOW_IPS",
        public_file, "forwarded_allow_ips", "127.0.0.1",
    )
    leader_only_raw = _pick(
        "leader_only", cli_leader_only, "BERTH_LEADER_ONLY",
        server_file, "leader_only", False,
    )
    leader_only = (
        str(leader_only_raw).lower() in {"1", "true", "yes"}
        if not isinstance(leader_only_raw, bool)
        else leader_only_raw
    )
    unsafe_deploy_raw = _pick(
        "allow_unsafe_deploy_options", cli_allow_unsafe_deploy_options,
        "BERTH_ALLOW_UNSAFE_DEPLOY_OPTIONS",
        server_file, "allow_unsafe_deploy_options", False,
    )
    allow_unsafe_deploy_options = (
        str(unsafe_deploy_raw).lower() in {"1", "true", "yes"}
        if not isinstance(unsafe_deploy_raw, bool)
        else unsafe_deploy_raw
    )
    leader_override = _env_get(env_map, "BERTH_LEADER_URL")
    if leader_override:
        source["leader_url"] = "env:BERTH_LEADER_URL"

    def _as_int(value: object) -> int:
        if isinstance(value, int):
            return value
        return int(str(value))

    def _as_path(value: object) -> Path:
        return Path(str(value))

    return ResolvedConfig(
        public_host=str(public_host),
        public_port=_as_int(public_port),
        public_bind=str(public_bind),
        cluster_host=str(cluster_host),
        cluster_port=_as_int(cluster_port),
        cluster_bind=str(cluster_bind),
        public_cert_path=_as_path(public_cert) if public_cert else None,
        public_key_path=_as_path(public_key) if public_key else None,
        public_scheme=str(public_scheme),
        trust_proxy_headers=bool(trust_proxy_headers),
        forwarded_allow_ips=str(forwarded_allow_ips),
        leader_url_override=leader_override,
        leader_only=bool(leader_only),
        allow_unsafe_deploy_options=bool(allow_unsafe_deploy_options),
        source=source,
    )
