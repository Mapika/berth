from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import ssl
import sys
from pathlib import Path

import structlog
import uvicorn

from berth import config
from berth.backends.base import Backend
from berth.backends.sglang import SGLangBackend
from berth.backends.trtllm import TRTLLMBackend
from berth.backends.vllm import VLLMBackend
from berth.cluster.ca import (
    ensure_server_cert,
    fingerprint_ca_pem,
    generate_ca,
    load_ca,
)
from berth.daemon.app import build_apps
from berth.lifecycle.docker_client import DockerClient
from berth.store import db

CONTROL_SOCKET_MODE = 0o600


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ]
    )


def _ensure_cluster_server_cert(
    serve_home: Path, hosts: list[str]
) -> tuple[Path, Path, str]:
    """Make sure the cluster CA exists and a server cert covering `hosts`
    is on disk. Returns (cert_path, key_path, ca_fingerprint)."""
    ca_dir = serve_home / "ca"
    if not (ca_dir / "ca.crt").exists():
        generate_ca(ca_dir, common_name="berth-ca")
    ca = load_ca(ca_dir)
    leader_dir = serve_home / "leader"
    crt_path = leader_dir / "server.crt"
    key_path = leader_dir / "server.key"
    required = list(dict.fromkeys([*hosts, "127.0.0.1", "localhost"]))
    ensure_server_cert(
        ca,
        crt_path=crt_path, key_path=key_path,
        required_hosts=required,
    )
    return crt_path, key_path, fingerprint_ca_pem(ca.cert_pem)


def _bind_control_socket(sock_path: Path) -> socket.socket:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        sock_path.unlink()
    except FileNotFoundError:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(sock_path))
        sock_path.chmod(CONTROL_SOCKET_MODE)
    except OSError:
        sock.close()
        raise
    return sock


async def serve(cfg: config.ResolvedConfig, sock_path: Path) -> None:
    log_ = logging.getLogger(__name__)
    config.ensure_private_dir(config.BERTH_DIR)
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    conn = db.connect(config.DB_PATH)
    db.init_schema(conn)

    # Operator footgun guard: binding globally but advertising loopback
    # produces enrollment URIs that nobody can reach. Warn loudly so
    # the operator notices before they paste the URI into a remote box.
    if cfg.public_bind in ("0.0.0.0", "::") and cfg.public_host.startswith("127."):  # nosec
        log_.warning(
            "advertising loopback host %s while binding globally on %s — "
            "enrollment URIs and the public_url will not be reachable from "
            "external clients. Set [public].host in ~/.berth/config.toml to "
            "your reachable address.",
            cfg.public_host, cfg.public_bind,
        )

    docker_client = DockerClient(network_name=config.DOCKER_NETWORK_NAME)
    docker_client.ensure_network()

    from berth.lifecycle.topology import read_topology
    topology = read_topology()
    log_.info(
        "topology: %d GPUs, islands=%s",
        len(topology.gpus),
        [list(topology.nvlink_island(g.index)) for g in topology.gpus],
    )

    from berth.backends.manifest import load_manifest
    manifest = load_manifest()
    backends: dict[str, Backend] = {
        "vllm": VLLMBackend(manifest["vllm"]),
        "sglang": SGLangBackend(manifest["sglang"]),
        "trtllm": TRTLLMBackend(manifest["trtllm"]),
    }

    # Cluster-CA-signed server cert covers both listeners (cluster cert
    # is also used as the public-listener fallback when no operator-supplied
    # cert is configured).
    cluster_crt, cluster_key, _ca_fp = _ensure_cluster_server_cert(
        config.BERTH_DIR,
        hosts=[cfg.cluster_host, cfg.public_host],
    )

    public_cert = cfg.public_cert_path
    public_key = cfg.public_key_path
    behind_proxy = cfg.public_scheme.lower() == "http"
    if behind_proxy:
        # Reverse-proxy mode: Caddy/Nginx terminates TLS upstream and
        # forwards to a plain-HTTP listener here.
        public_cert = None
        public_key = None
        log_.info(
            "public listener in reverse-proxy mode (plain HTTP). "
            "Reverse proxy at %s must terminate TLS and forward to %s:%d.",
            cfg.forwarded_allow_ips, cfg.public_bind, cfg.public_port,
        )
    elif public_cert is None or public_key is None:
        public_cert = cluster_crt
        public_key = cluster_key
        log_.warning(
            "public listener using cluster-CA cert (no [public_tls] configured). "
            "External clients must trust %s — set [public_tls] in ~/.berth/config.toml "
            "for browser/SDK use.", _ca_fp,
        )
    else:
        if not Path(public_cert).exists() or not Path(public_key).exists():
            raise FileNotFoundError(
                f"public TLS cert/key not found: cert={public_cert}, key={public_key}"
            )

    public_app, cluster_app, uds_app = build_apps(
        conn=conn,
        docker_client=docker_client,
        backends=backends,
        models_dir=config.MODELS_DIR,
        topology=topology,
        configs_dir=config.CONFIGS_DIR,
        leader_url=cfg.cluster_url,
        resolved_cfg=cfg,
    )

    uds_socket = _bind_control_socket(sock_path)

    public_uvicorn_kwargs: dict = {
        "app": public_app,
        "host": cfg.public_bind,
        "port": cfg.public_port,
        "log_level": "info",
    }
    if behind_proxy:
        public_uvicorn_kwargs["proxy_headers"] = True
        public_uvicorn_kwargs["forwarded_allow_ips"] = cfg.forwarded_allow_ips
    else:
        public_uvicorn_kwargs["ssl_keyfile"] = str(public_key)
        public_uvicorn_kwargs["ssl_certfile"] = str(public_cert)
        if cfg.trust_proxy_headers:
            # Operator opted-in even with our own TLS (e.g. CDN that also
            # terminates TLS). Honour the headers.
            public_uvicorn_kwargs["proxy_headers"] = True
            public_uvicorn_kwargs["forwarded_allow_ips"] = cfg.forwarded_allow_ips
    public_cfg = uvicorn.Config(**public_uvicorn_kwargs)
    from berth.cluster.tls_ws_protocol import TLSAwareWebSocketProtocol

    cluster_cfg = uvicorn.Config(
        app=cluster_app,
        host=cfg.cluster_bind, port=cfg.cluster_port,
        ssl_keyfile=str(cluster_key), ssl_certfile=str(cluster_crt),
        ssl_ca_certs=str(config.BERTH_DIR / "ca" / "ca.crt"),
        ssl_cert_reqs=ssl.CERT_OPTIONAL,
        ws=TLSAwareWebSocketProtocol,
        log_level="info",
    )
    uds_cfg = uvicorn.Config(app=uds_app, log_level="info")
    public_server = uvicorn.Server(public_cfg)
    cluster_server = uvicorn.Server(cluster_cfg)
    uds_server = uvicorn.Server(uds_cfg)
    await asyncio.gather(
        public_server.serve(),
        cluster_server.serve(),
        uds_server.serve(sockets=[uds_socket]),
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="berth-daemon")
    p.add_argument("--public-host")
    p.add_argument("--public-port", type=int)
    p.add_argument("--public-bind")
    p.add_argument("--public-cert")
    p.add_argument("--public-key")
    p.add_argument("--cluster-host")
    p.add_argument("--cluster-port", type=int)
    p.add_argument("--cluster-bind")
    p.add_argument("--sock", default=str(config.SOCK_PATH))
    # Back-compat aliases: --host/--port map to --public-host/--public-port.
    p.add_argument("--host", dest="public_host_alias")
    p.add_argument("--port", type=int, dest="public_port_alias")
    args = p.parse_args(argv)

    configure_logging()
    cfg = config.resolve_config(
        cli_public_host=args.public_host or args.public_host_alias,
        cli_public_port=args.public_port or args.public_port_alias,
        cli_public_bind=args.public_bind,
        cli_cluster_host=args.cluster_host,
        cli_cluster_port=args.cluster_port,
        cli_cluster_bind=args.cluster_bind,
        cli_public_cert=args.public_cert,
        cli_public_key=args.public_key,
    )
    asyncio.run(serve(cfg, Path(args.sock)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
