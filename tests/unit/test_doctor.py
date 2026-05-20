from berth.doctor import runner
from berth.doctor.checks import (
    check_docker,
    check_gpus,
    check_paths,
    check_ports,
)
from berth.doctor.runner import CheckResult


def test_check_paths_writable(tmp_path, monkeypatch):
    monkeypatch.setattr("berth.doctor.checks.BERTH_DIR", tmp_path)
    r = check_paths()
    assert r.status == "ok"
    assert "writable" in r.detail.lower()


def test_check_paths_not_writable(tmp_path, monkeypatch):
    bad = tmp_path / "bad"
    bad.mkdir()
    bad.chmod(0o400)  # read-only
    monkeypatch.setattr("berth.doctor.checks.BERTH_DIR", bad)
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


def test_run_all_leader_only_skips_local_runtime_checks(monkeypatch):
    calls: list[str] = []

    def ok(name: str):
        def _check():
            calls.append(name)
            return CheckResult(name=name, status="ok", detail="ok")
        return _check

    def forbidden():
        raise AssertionError("local runtime check should be skipped")

    monkeypatch.setattr(runner, "check_paths", ok("paths"))
    monkeypatch.setattr(runner, "check_ports", ok("ports"))
    monkeypatch.setattr(runner, "check_hf_token", ok("hf"))
    monkeypatch.setattr(runner, "check_docker", forbidden)
    monkeypatch.setattr(runner, "check_gpus", forbidden)
    monkeypatch.setattr(runner, "check_engine_images", forbidden)

    results = runner.run_all(leader_only=True)

    assert calls == ["paths", "ports", "hf"]
    assert [r.name for r in results] == ["paths", "ports", "hf"]
