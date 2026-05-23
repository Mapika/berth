from berth.cluster import adopted
from berth.cluster.agent_client import _recompute_alive


def _e(cid, port=1):
    return adopted.AdoptedEndpoint(
        name=cid, model_name="m", served_model_name="m", address="127.0.0.1",
        port=port, container_id=cid, gpu_ids=[0], vram_reserved_mb=1, image_tag="x")


def test_alive_stays_true_on_single_failure():
    entries = [_e("a")]
    fails = {}
    alive = {}
    _recompute_alive(entries, fails, alive, probe=lambda a, p: True)
    changed = _recompute_alive(entries, fails, alive, probe=lambda a, p: False)
    assert alive["a"] is True and changed is False and fails["a"] == 1


def test_two_failures_flip_to_down_then_recover():
    entries = [_e("a")]
    fails = {}
    alive = {"a": True}
    _recompute_alive(entries, fails, alive, probe=lambda a, p: False)
    changed = _recompute_alive(entries, fails, alive, probe=lambda a, p: False)
    assert alive["a"] is False and changed is True
    changed = _recompute_alive(entries, fails, alive, probe=lambda a, p: True)
    assert alive["a"] is True and changed is True and fails["a"] == 0


def test_removed_endpoint_is_pruned():
    fails = {"a": 0}
    alive = {"a": True}
    changed = _recompute_alive([], fails, alive, probe=lambda a, p: True)
    assert "a" not in alive and "a" not in fails and changed is True
