from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from serve_engine.config import DEFAULT_PUBLIC_PORT, SERVE_DIR

try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import pynvml  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    pynvml = None  # type: ignore[assignment]

try:
    import docker  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    docker = None  # type: ignore[assignment]


# Module-level for monkeypatching in tests.
DEFAULT_PORT = DEFAULT_PUBLIC_PORT


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str | None = None


def _docker_from_env() -> Any:
    if docker is None:
        raise RuntimeError("docker SDK not installed")
    return cast(Any, docker).from_env()


def check_paths() -> CheckResult:
    p = Path(SERVE_DIR)
    p.mkdir(parents=True, exist_ok=True)
    if not os.access(p, os.W_OK):
        return CheckResult(
            name="serve directory",
            status="fail",
            detail=f"{p} is not writable",
            fix=f"chmod u+w {p}",
        )
    return CheckResult(name="serve directory", status="ok", detail=f"{p} writable")


def check_ports() -> CheckResult:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", DEFAULT_PORT))
        return CheckResult(
            name=f"port {DEFAULT_PORT}",
            status="ok",
            detail=f"port {DEFAULT_PORT} is free",
        )
    except OSError as e:
        return CheckResult(
            name=f"port {DEFAULT_PORT}",
            status="fail",
            detail=f"port {DEFAULT_PORT} bind failed: {e}",
            fix=f"identify the process with `ss -lntp | grep :{DEFAULT_PORT}` and stop it",
        )
    finally:
        s.close()


def check_docker() -> CheckResult:
    try:
        client = _docker_from_env()
        client.ping()
    except Exception as e:
        return CheckResult(
            name="docker",
            status="fail",
            detail=f"docker daemon unreachable: {e}",
            fix="ensure Docker is running and your user is in the 'docker' group",
        )
    try:
        info = client.info()
        version = client.version().get("Version", "?")
        runtimes = info.get("Runtimes", {})
        # Docker 28+ uses CDI for GPU access and no longer advertises an
        # `nvidia` runtime in `docker info`. We probe with a short-lived
        # container that requests a GPU; if it starts, GPU support works
        # regardless of the runtime registry.
        if "nvidia" in runtimes:
            return CheckResult(
                name="docker",
                status="ok",
                detail=f"docker {version} with nvidia runtime",
            )
        try:
            client.containers.run(
                "alpine:latest",
                command=["true"],
                device_requests=[{"Driver": "nvidia", "Count": 1, "Capabilities": [["gpu"]]}],
                remove=True,
            )
            return CheckResult(
                name="docker",
                status="ok",
                detail=f"docker {version} (GPU device_requests work; CDI mode)",
            )
        except Exception as e:
            return CheckResult(
                name="docker",
                status="warn",
                detail=f"docker {version} OK, but GPU device_requests failed: {e}",
                fix="install nvidia-container-toolkit and restart dockerd",
            )
    except Exception as e:
        return CheckResult(
            name="docker", status="warn", detail=f"docker reachable but info failed: {e}"
        )


def check_gpus() -> CheckResult:
    if pynvml is None:
        return CheckResult(
            name="gpus",
            status="fail",
            detail="pynvml not available; cannot enumerate GPUs",
            fix="pip install pynvml",
        )
    try:
        pynvml.nvmlInit()
        n = pynvml.nvmlDeviceGetCount()
    except Exception as e:
        return CheckResult(
            name="gpus",
            status="fail",
            detail=f"NVML init failed: {e}",
            fix="install NVIDIA driver matching your CUDA runtime",
        )
    if n == 0:
        return CheckResult(name="gpus", status="fail", detail="no GPUs detected")
    names: list[str] = []
    total_mb = 0
    for i in range(n):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        nm = pynvml.nvmlDeviceGetName(h)
        nm = nm.decode() if isinstance(nm, bytes) else str(nm)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        names.append(nm)
        total_mb += int(mem.total) // 1024 // 1024
    summary = f"{n} GPU(s): {', '.join(names)} (total {total_mb} MB)"
    return CheckResult(name="gpus", status="ok", detail=summary)


def check_hf_token() -> CheckResult:
    if os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"):
        return CheckResult(name="HF token", status="ok", detail="HF_TOKEN set")
    return CheckResult(
        name="HF token",
        status="warn",
        detail="HF_TOKEN not set; gated models will fail to download",
        fix="export HF_TOKEN=hf_xxx (https://huggingface.co/settings/tokens)",
    )


def check_engine_images() -> CheckResult:
    """Check whether each registered engine's image is cached locally."""
    from serve_engine.backends.manifest import load_manifest
    try:
        client = _docker_from_env()
        tags: set[str] = set()
        for img in client.images.list():
            tags.update(img.tags or [])
    except Exception:
        return CheckResult(
            name="engine images",
            status="warn",
            detail="docker not reachable; cannot inspect images",
        )
    found = []
    missing = []
    for engine in load_manifest().values():
        prefix = f"{engine.image}:"
        hit = [t for t in tags if t.startswith(prefix)]
        if hit:
            found.append(hit[0])
        else:
            missing.append(engine.image)
    if missing:
        return CheckResult(
            name="engine images",
            status="warn",
            detail=f"cached: {found or 'none'}; missing: {missing}",
            fix="serve will pull on first use; or `docker pull <image>` ahead of time",
        )
    return CheckResult(
        name="engine images",
        status="ok",
        detail=f"cached: {found}",
    )
