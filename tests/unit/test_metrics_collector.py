from __future__ import annotations

from serve_engine.cluster import metrics_collector as mc


def test_in_flight_counter_starts_at_zero():
    c = mc.InFlightCounter()
    assert c.snapshot() == {}


def test_in_flight_counter_increments_and_decrements_by_deployment():
    c = mc.InFlightCounter()
    c.start(7)
    c.start(7)
    c.start(9)
    assert c.snapshot() == {7: 2, 9: 1}
    c.finish(7)
    assert c.snapshot() == {7: 1, 9: 1}


def test_in_flight_counter_never_goes_negative():
    c = mc.InFlightCounter()
    c.finish(7)
    assert c.snapshot() == {}


def test_latency_recorder_buckets_recent_samples():
    r = mc.LatencyRecorder()
    r.record(deployment_id=7, latency_ms=100, error=False)
    r.record(deployment_id=7, latency_ms=200, error=False)
    r.record(deployment_id=7, latency_ms=300, error=True)
    summary = r.summarize_and_reset(7)
    assert summary.requests_last_window == 3
    assert summary.errors_last_window == 1
    # p50 of [100,200,300] via nearest-rank (rank index = round(0.5*2) = 1) → 200.
    # p95 with n=3 → rank = round(0.95*2) = 2 → 300.
    assert summary.latency_p50_ms == 200
    assert summary.latency_p95_ms == 300


def test_latency_recorder_resets_after_summarize():
    r = mc.LatencyRecorder()
    r.record(deployment_id=7, latency_ms=100, error=False)
    r.summarize_and_reset(7)
    summary = r.summarize_and_reset(7)
    assert summary.requests_last_window == 0
    assert summary.latency_p50_ms == 0
    assert summary.latency_p95_ms == 0


def test_build_snapshot_assembles_all_three_sections(monkeypatch):
    from serve_engine.observability.gpu_stats import GPUSnapshot

    monkeypatch.setattr(
        mc, "read_gpu_stats",
        lambda: [GPUSnapshot(index=0, memory_used_mb=2048, memory_total_mb=81920,
                             gpu_util_pct=42, power_w=120)],
    )
    in_flight = mc.InFlightCounter()
    in_flight.start(7)
    latency = mc.LatencyRecorder()
    latency.record(deployment_id=7, latency_ms=150, error=False)

    snap = mc.build_snapshot(
        in_flight=in_flight, latency=latency,
        deployment_models={7: "llama3-8b"},
        uptime_s=42.0,
    )
    assert snap["gpus"][0]["util_pct"] == 42
    assert snap["deployments"][0]["deployment_id"] == 7
    assert snap["deployments"][0]["model_id"] == "llama3-8b"
    assert snap["deployments"][0]["in_flight"] == 1
    assert snap["deployments"][0]["requests_last_window"] == 1
    assert snap["node"]["uptime_s"] == 42.0
