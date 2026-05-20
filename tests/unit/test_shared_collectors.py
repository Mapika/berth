"""build_apps must share one InFlightCounter + LatencyRecorder pair
across public_app, cluster_app, uds_app — otherwise the local-metrics
ticker samples different counters than the proxy increments."""
from __future__ import annotations

from unittest.mock import MagicMock

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_apps
from berth.store import db


def test_three_apps_share_collector_instances(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    public_app, cluster_app, uds_app = build_apps(
        conn=conn, docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
    )
    assert (
        public_app.state.in_flight
        is cluster_app.state.in_flight
        is uds_app.state.in_flight
    )
    assert (
        public_app.state.latency
        is cluster_app.state.latency
        is uds_app.state.latency
    )


def test_build_app_still_has_collectors_attached(tmp_path):
    """Single-app build_app callers get standalone collector instances."""
    from berth.daemon.app import build_app

    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    app = build_app(
        conn=conn, docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
    )
    assert app.state.in_flight is not None
    assert app.state.latency is not None
