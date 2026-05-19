from __future__ import annotations

from berth.doctor.checks import (
    CheckResult,
    check_docker,
    check_engine_images,
    check_gpus,
    check_hf_token,
    check_paths,
    check_ports,
)


def run_all() -> list[CheckResult]:
    return [
        check_paths(),
        check_ports(),
        check_docker(),
        check_gpus(),
        check_hf_token(),
        check_engine_images(),
    ]


def summarise(results: list[CheckResult]) -> tuple[int, int, int]:
    ok = sum(1 for r in results if r.status == "ok")
    warn = sum(1 for r in results if r.status == "warn")
    fail = sum(1 for r in results if r.status == "fail")
    return ok, warn, fail
