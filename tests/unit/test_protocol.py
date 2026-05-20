from __future__ import annotations

import pytest

from berth.cluster.protocol import (
    GpuStats,
    Heartbeat,
    Hello,
    HttpCancel,
    HttpChunk,
    HttpRequest,
    OpResult,
    StartDeployment,
    StopDeployment,
    Welcome,
    decode_frame,
    encode_frame,
)


def test_encode_decode_hello():
    f = Hello(agent_version="0.0.1",
              host_info={"cpu_count": 4, "total_ram_mb": 8000, "gpus": []})
    back = decode_frame(encode_frame(f))
    assert isinstance(back, Hello)
    assert back.agent_version == "0.0.1"
    assert back.host_info["cpu_count"] == 4


def test_decode_unknown_type_raises():
    with pytest.raises(ValueError):
        decode_frame('{"type": "nope"}')


def test_decode_missing_type_raises():
    with pytest.raises(ValueError):
        decode_frame('{"agent_version": "x"}')


def test_decode_bytes_accepted():
    f = Heartbeat(ts=42.0)
    assert isinstance(decode_frame(encode_frame(f).encode()), Heartbeat)


def test_welcome_roundtrip():
    f = Welcome(node_id=7, server_time=123.4)
    back = decode_frame(encode_frame(f))
    assert isinstance(back, Welcome)
    assert back.node_id == 7
    assert back.server_time == 123.4


def test_gpu_stats_roundtrip():
    f = GpuStats(gpus=[{"index": 0, "used_mb": 1024, "util": 0.5}])
    back = decode_frame(encode_frame(f))
    assert isinstance(back, GpuStats)
    assert back.gpus[0]["index"] == 0


def test_start_and_stop_deployment_roundtrip():
    s = StartDeployment(request_id="r1", plan={"image": "x"})
    assert isinstance(decode_frame(encode_frame(s)), StartDeployment)
    p = StopDeployment(request_id="r2", container_id="cid")
    back = decode_frame(encode_frame(p))
    assert isinstance(back, StopDeployment)
    assert back.container_id == "cid"


def test_op_result_with_data():
    f = OpResult(request_id="r", ok=True, data={"container_id": "x"})
    back = decode_frame(encode_frame(f))
    assert isinstance(back, OpResult)
    assert back.ok is True
    assert back.data["container_id"] == "x"
    assert back.error is None


def test_op_result_with_error():
    f = OpResult(request_id="r", ok=False, error="boom")
    back = decode_frame(encode_frame(f))
    assert isinstance(back, OpResult)
    assert back.ok is False
    assert back.error == "boom"
    assert back.data is None


def test_http_request_roundtrip():
    f = HttpRequest(
        stream_id="s1", method="POST", path="/v1/chat/completions",
        headers={"content-type": "application/json"}, body_b64="aGVsbG8=",
    )
    back = decode_frame(encode_frame(f))
    assert isinstance(back, HttpRequest)
    assert back.method == "POST"
    assert back.body_b64 == "aGVsbG8="


def test_http_chunk_first_with_status():
    f = HttpChunk(stream_id="s1", body_b64="dGVzdA==", eof=False,
                  status=200, headers={"x": "y"})
    back = decode_frame(encode_frame(f))
    assert isinstance(back, HttpChunk)
    assert back.status == 200
    assert back.headers == {"x": "y"}
    assert back.eof is False


def test_http_chunk_eof():
    f = HttpChunk(stream_id="s1", body_b64="", eof=True)
    back = decode_frame(encode_frame(f))
    assert isinstance(back, HttpChunk)
    assert back.eof is True
    assert back.status is None
    assert back.headers is None


def test_http_cancel_roundtrip():
    f = HttpCancel(stream_id="s1")
    back = decode_frame(encode_frame(f))
    assert isinstance(back, HttpCancel)
    assert back.stream_id == "s1"


def test_heartbeat_round_trips_without_metrics():
    hb = Heartbeat(ts=1234.5)
    decoded = decode_frame(encode_frame(hb))
    assert isinstance(decoded, Heartbeat)
    assert decoded.ts == 1234.5
    assert decoded.metrics is None


def test_heartbeat_round_trips_with_metrics():
    hb = Heartbeat(
        ts=1234.5,
        metrics={
            "gpus": [{"index": 0, "mem_used_mb": 1024, "mem_total_mb": 81920,
                      "util_pct": 42, "temp_c": 55}],
            "deployments": [{"deployment_id": 7, "model_id": "llama3-8b",
                             "in_flight": 3, "requests_last_window": 12,
                             "latency_p50_ms": 120, "latency_p95_ms": 450,
                             "errors_last_window": 0}],
            "node": {"uptime_s": 99.0, "host_load_avg_1m": 0.8},
        },
    )
    decoded = decode_frame(encode_frame(hb))
    assert isinstance(decoded, Heartbeat)
    assert decoded.metrics is not None
    assert decoded.metrics["gpus"][0]["util_pct"] == 42
    assert decoded.metrics["deployments"][0]["in_flight"] == 3


def test_heartbeat_wire_format_decodes():
    raw = '{"type": "heartbeat", "ts": 1234.5}'
    decoded = decode_frame(raw)
    assert isinstance(decoded, Heartbeat)
    assert decoded.metrics is None
