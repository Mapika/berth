from berth.doctor.checks import (
    check_docker,
    check_gpus,
    check_paths,
    check_ports,
)


def test_check_paths_writable(tmp_path, monkeypatch):
    monkeypatch.setattr("berth.doctor.checks.SERVE_DIR", tmp_path)
    r = check_paths()
    assert r.status == "ok"
    assert "writable" in r.detail.lower()


def test_check_paths_not_writable(tmp_path, monkeypatch):
    bad = tmp_path / "bad"
    bad.mkdir()
    bad.chmod(0o400)  # read-only
    monkeypatch.setattr("berth.doctor.checks.SERVE_DIR", bad)
    r = check_paths()
    assert r.status in ("warn", "fail")
    bad.chmod(0o755)  # restore for cleanup


def test_check_ports_free(monkeypatch):
    monkeypatch.setattr("berth.doctor.checks.DEFAULT_PORT", 0)
    r = check_ports()
    # Port 0 always binds; check returns ok
    assert r.status == "ok"


def test_check_docker_unreachable(monkeypatch):
    def fake_docker_from_env():
        raise RuntimeError("connection refused")
    monkeypatch.setattr("berth.doctor.checks._docker_from_env", fake_docker_from_env)
    r = check_docker()
    assert r.status == "fail"
    assert "docker" in r.detail.lower()


def test_check_gpus_no_pynvml(monkeypatch):
    monkeypatch.setattr("berth.doctor.checks.pynvml", None)
    r = check_gpus()
    assert r.status == "fail"
    assert "pynvml" in r.detail.lower() or "no" in r.detail.lower()
