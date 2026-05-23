from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml


class AdoptError(Exception):
    """Raised for invalid adopt requests (collision, unreachable endpoint)."""


@dataclass
class AdoptedEndpoint:
    name: str
    model_name: str
    served_model_name: str
    address: str
    port: int
    container_id: str
    gpu_ids: list[int] = field(default_factory=list)
    vram_reserved_mb: int = 0
    image_tag: str = "external"

    def to_report_dict(self, *, alive: bool) -> dict[str, Any]:
        d = asdict(self)
        d.pop("name")  # 'name' is a local display label, not reported
        d["alive"] = alive
        return d


def _path(home: Path) -> Path:
    return home / "adopted.yaml"


def load(home: Path) -> list[AdoptedEndpoint]:
    p = _path(home)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or []
    return [AdoptedEndpoint(**e) for e in raw]


def save(home: Path, entries: list[AdoptedEndpoint]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    _path(home).write_text(
        yaml.safe_dump([asdict(e) for e in entries], sort_keys=False)
    )


def add_entry(home: Path, entry: AdoptedEndpoint) -> list[AdoptedEndpoint]:
    entries = load(home)
    if any(e.name == entry.name for e in entries):
        raise AdoptError(f"adopted name {entry.name!r} already exists")
    used = {g for e in entries for g in e.gpu_ids}
    clash = sorted(used.intersection(entry.gpu_ids))
    if clash:
        raise AdoptError(f"GPU(s) {clash} already used by another adopted endpoint")
    entries.append(entry)
    save(home, entries)
    return entries


def remove_entry(home: Path, name: str) -> list[AdoptedEndpoint]:
    entries = [e for e in load(home) if e.name != name]
    save(home, entries)
    return entries


def probe_served_model(address: str, port: int, *, timeout: float = 5.0) -> str:
    """GET /v1/models; return the first model id. Raises AdoptError if the
    endpoint is unreachable or returns no model."""
    url = f"http://{address}:{port}/v1/models"
    try:
        r = httpx.get(url, timeout=timeout)
        data = r.json().get("data") or []
        if not data:
            raise AdoptError(f"{url} returned no models")
        return str(data[0]["id"])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise AdoptError(f"endpoint {address}:{port} not reachable: {e}") from e


def introspect_container(dc, name: str) -> tuple[str, str, int, list[int], str]:
    """Resolve a running docker container by name to
    (container_id, host_address, host_port, gpu_ids, image_tag).

    `dc` is a berth DockerClient exposing `._client` (docker SDK)."""
    c = dc._client.containers.get(name)  # raises docker.errors.NotFound
    attrs = c.attrs
    ports = (attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
    host_addr, host_port = "127.0.0.1", None
    for _internal, bindings in ports.items():
        if bindings:
            host_port = int(bindings[0]["HostPort"])
            addr = bindings[0].get("HostIp") or "127.0.0.1"
            host_addr = "127.0.0.1" if addr in ("0.0.0.0", "") else addr
            break
    if host_port is None:
        raise AdoptError(f"container {name!r} has no published host port")
    host_cfg = attrs.get("HostConfig", {}) or {}
    gpu_ids: list[int] = []
    for req in host_cfg.get("DeviceRequests") or []:
        for dev in req.get("DeviceIDs") or []:
            if str(dev).isdigit():
                gpu_ids.append(int(dev))
    image_tag = (attrs.get("Config", {}) or {}).get("Image", "external")
    return c.id, host_addr, host_port, gpu_ids, image_tag
