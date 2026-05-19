from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class Hello:
    agent_version: str
    host_info: dict[str, Any]
    type: str = "hello"


@dataclass
class Welcome:
    node_id: int
    server_time: float
    type: str = "welcome"


@dataclass
class Heartbeat:
    ts: float
    metrics: dict[str, Any] | None = None
    type: str = "heartbeat"


@dataclass
class GpuStats:
    gpus: list[dict[str, Any]]
    type: str = "gpu_stats"


@dataclass
class StartDeployment:
    request_id: str
    plan: dict[str, Any]
    type: str = "start_deployment"


@dataclass
class StopDeployment:
    request_id: str
    container_id: str
    type: str = "stop_deployment"


@dataclass
class OpResult:
    request_id: str
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    type: str = "op_result"


@dataclass
class HttpRequest:
    stream_id: str
    method: str
    path: str
    headers: dict[str, str]
    body_b64: str
    type: str = "http_request"


@dataclass
class HttpChunk:
    stream_id: str
    body_b64: str
    eof: bool
    status: int | None = None
    headers: dict[str, str] | None = None
    type: str = "http_chunk"


@dataclass
class HttpCancel:
    stream_id: str
    type: str = "http_cancel"


@dataclass
class LogStream:
    """Leader → agent: start streaming docker logs for a container.

    `tail` is the number of historical lines to ship before the live tail;
    `follow=True` means keep streaming until LogCancel."""
    stream_id: str
    container_id: str
    tail: int = 500
    follow: bool = True
    type: str = "log_stream"


@dataclass
class LogChunk:
    """Agent → leader: a slice of docker log output.

    `body_b64` carries raw bytes (mixed stdout/stderr). The final chunk
    has `eof=True` and typically empty body."""
    stream_id: str
    body_b64: str
    eof: bool
    type: str = "log_chunk"


@dataclass
class LogCancel:
    """Leader → agent: stop a log stream (client disconnected)."""
    stream_id: str
    type: str = "log_cancel"


Frame = (
    Hello | Welcome | Heartbeat | GpuStats
    | StartDeployment | StopDeployment | OpResult
    | HttpRequest | HttpChunk | HttpCancel
    | LogStream | LogChunk | LogCancel
)


_REGISTRY: dict[str, type] = {
    "hello": Hello,
    "welcome": Welcome,
    "heartbeat": Heartbeat,
    "gpu_stats": GpuStats,
    "start_deployment": StartDeployment,
    "stop_deployment": StopDeployment,
    "op_result": OpResult,
    "http_request": HttpRequest,
    "http_chunk": HttpChunk,
    "http_cancel": HttpCancel,
    "log_stream": LogStream,
    "log_chunk": LogChunk,
    "log_cancel": LogCancel,
}


def encode_frame(frame: Frame) -> str:
    return json.dumps(frame.__dict__)


def decode_frame(wire: str | bytes) -> Frame:
    if isinstance(wire, bytes | bytearray):
        wire = bytes(wire).decode("utf-8")
    raw = json.loads(wire)
    if not isinstance(raw, dict) or "type" not in raw:
        raise ValueError("frame missing 'type'")
    cls = _REGISTRY.get(raw["type"])
    if cls is None:
        raise ValueError(f"unknown frame type: {raw['type']!r}")
    payload = {k: v for k, v in raw.items() if k != "type"}
    return cls(**payload)
