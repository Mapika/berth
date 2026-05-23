import httpx
import pytest

from berth.cluster import adopted


def test_save_load_round_trip(tmp_path):
    e = adopted.AdoptedEndpoint(
        name="minimax", model_name="nvidia/MiniMax-M2.7-NVFP4",
        served_model_name="nvidia/MiniMax-M2.7-NVFP4",
        address="127.0.0.1", port=30011, container_id="cid-1",
        gpu_ids=[7], vram_reserved_mb=268000, image_tag="external",
    )
    adopted.save(tmp_path, [e])
    loaded = adopted.load(tmp_path)
    assert loaded == [e]


def test_add_entry_rejects_name_collision(tmp_path):
    e = adopted.AdoptedEndpoint(
        name="minimax", model_name="m", served_model_name="m",
        address="127.0.0.1", port=30011, container_id="c",
        gpu_ids=[7], vram_reserved_mb=1, image_tag="external")
    adopted.save(tmp_path, [e])
    with pytest.raises(adopted.AdoptError, match="name"):
        adopted.add_entry(tmp_path, e)


def test_add_entry_rejects_gpu_overlap(tmp_path):
    a = adopted.AdoptedEndpoint(
        name="a", model_name="a", served_model_name="a",
        address="127.0.0.1", port=1, container_id="ca",
        gpu_ids=[7], vram_reserved_mb=1, image_tag="external")
    b = adopted.AdoptedEndpoint(
        name="b", model_name="b", served_model_name="b",
        address="127.0.0.1", port=2, container_id="cb",
        gpu_ids=[7], vram_reserved_mb=1, image_tag="external")
    adopted.save(tmp_path, [a])
    with pytest.raises(adopted.AdoptError, match="GPU"):
        adopted.add_entry(tmp_path, b)


def test_probe_returns_served_model(monkeypatch):
    def fake_get(url, timeout):
        assert url.endswith("/v1/models")
        return httpx.Response(
            200,
            json={"data": [{"id": "served-x"}]},
            request=httpx.Request("GET", url),
        )
    monkeypatch.setattr(adopted.httpx, "get", fake_get)
    assert adopted.probe_served_model("127.0.0.1", 30011) == "served-x"


def test_probe_raises_on_error_status(monkeypatch):
    def fake_get(url, timeout):
        return httpx.Response(
            503,
            json={"error": "overloaded"},
            request=httpx.Request("GET", url),
        )
    monkeypatch.setattr(adopted.httpx, "get", fake_get)
    with pytest.raises(adopted.AdoptError, match="not reachable"):
        adopted.probe_served_model("127.0.0.1", 30011)


def test_probe_raises_when_unreachable(monkeypatch):
    def boom(url, timeout):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(adopted.httpx, "get", boom)
    with pytest.raises(adopted.AdoptError, match="not reachable"):
        adopted.probe_served_model("127.0.0.1", 30011)
