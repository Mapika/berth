from typer.testing import CliRunner

from berth.cli import app
from berth.cluster import adopted

runner = CliRunner()


def test_adopt_by_port_writes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    monkeypatch.setattr(adopted, "probe_served_model",
                        lambda a, p, **k: "nvidia/MiniMax-M2.7-NVFP4")
    result = runner.invoke(app, [
        "agent", "adopt",
        "--port", "30011", "--model", "nvidia/MiniMax-M2.7-NVFP4",
        "--name", "minimax", "--gpus", "7", "--vram-mb", "268000",
    ])
    assert result.exit_code == 0, result.output
    entries = adopted.load(tmp_path)
    assert len(entries) == 1
    assert entries[0].port == 30011
    assert entries[0].gpu_ids == [7]
    assert entries[0].container_id == "adopted-nvidia-MiniMax-M2.7-NVFP4-30011"


def test_adopt_aborts_when_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    def boom(a, p, **k):
        raise adopted.AdoptError("not reachable")
    monkeypatch.setattr(adopted, "probe_served_model", boom)
    result = runner.invoke(app, [
        "agent", "adopt", "--port", "30011", "--model", "m",
    ])
    assert result.exit_code != 0
    assert adopted.load(tmp_path) == []


def test_unadopt_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    e = adopted.AdoptedEndpoint(
        name="minimax", model_name="m", served_model_name="m",
        address="127.0.0.1", port=30011, container_id="c",
        gpu_ids=[7], vram_reserved_mb=1, image_tag="external")
    adopted.save(tmp_path, [e])
    result = runner.invoke(app, ["agent", "unadopt", "minimax"])
    assert result.exit_code == 0, result.output
    assert adopted.load(tmp_path) == []


def test_adopted_ls_shows_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    e = adopted.AdoptedEndpoint(
        name="minimax", model_name="m", served_model_name="served-m",
        address="127.0.0.1", port=30011, container_id="c",
        gpu_ids=[7], vram_reserved_mb=1, image_tag="external")
    adopted.save(tmp_path, [e])
    result = runner.invoke(app, ["agent", "adopted"])
    assert result.exit_code == 0, result.output
    assert "minimax" in result.output
    assert "served-m" in result.output
