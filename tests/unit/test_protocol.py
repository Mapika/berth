from __future__ import annotations

import pytest

from serve_engine.cluster.protocol import (
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
