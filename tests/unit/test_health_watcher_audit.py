from __future__ import annotations

from berth.cluster.health_watcher import on_node_unreachable
from berth.routing.affinity import RoutingAffinity


def test_node_unreachable_evicts_affinity_entries():
    aff = RoutingAffinity(capacity=4)
    aff.set("k1", node_id=10)
    aff.set("k2", node_id=11)
    on_node_unreachable(node_id=10, label="worker-a", affinity=aff)
    assert aff.lookup("k1") is None
    assert aff.lookup("k2") == 11


def test_node_unreachable_logs_audit_line(caplog):
    aff = RoutingAffinity(capacity=4)
    with caplog.at_level("WARNING"):
        on_node_unreachable(node_id=10, label="worker-a", affinity=aff)
    joined = " ".join(r.message for r in caplog.records)
    assert "worker-a" in joined
    assert "node_loss_audit" in joined


def test_on_node_unreachable_without_affinity_is_a_noop():
    """Tests that don't construct an affinity instance must still
    survive the audit hook."""
    on_node_unreachable(node_id=10, label="worker-a", affinity=None)
