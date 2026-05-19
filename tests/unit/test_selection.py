from pathlib import Path

from berth.backends.selection import (
    load_selection,
    pick_backend,
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "selection.yaml"
    p.write_text(content)
    return p


def test_default_picks_vllm(tmp_path):
    cfg = load_selection(_write(tmp_path, "rules: []\ndefault: vllm\n"))
    assert pick_backend(cfg, "Llama-3.1-70B-Instruct") == "vllm"


def test_pattern_match(tmp_path):
    cfg = load_selection(_write(
        tmp_path,
        "rules:\n"
        "  - pattern: '*deepseek-v3*'\n"
        "    backend: sglang\n"
        "default: vllm\n",
    ))
    assert pick_backend(cfg, "deepseek-v3-671b") == "sglang"
    assert pick_backend(cfg, "Llama-3.1-70B") == "vllm"


def test_first_match_wins(tmp_path):
    cfg = load_selection(_write(
        tmp_path,
        "rules:\n"
        "  - pattern: '*qwen*'\n"
        "    backend: sglang\n"
        "  - pattern: '*qwen*-vl*'\n"
        "    backend: vllm\n"
        "default: vllm\n",
    ))
    # First rule matches qwen-2.5-vl-7b too; first match wins
    assert pick_backend(cfg, "qwen-2.5-vl-7b") == "sglang"


def test_case_insensitive(tmp_path):
    cfg = load_selection(_write(
        tmp_path,
        "rules:\n"
        "  - pattern: '*DEEPSEEK*'\n"
        "    backend: sglang\n"
        "default: vllm\n",
    ))
    assert pick_backend(cfg, "deepseek-v3") == "sglang"


def test_load_default_path_uses_package_resource():
    cfg = load_selection()
    assert cfg.default in ("vllm", "sglang", "trtllm")
    assert isinstance(cfg.rules, list)


def test_packaged_rules_route_nvfp4_and_nvidia_to_trtllm():
    """Packaged selection.yaml ships sane defaults for TRT-LLM checkpoints."""
    cfg = load_selection()
    assert pick_backend(cfg, "nvidia/Llama-4-Maverick-17B-128E-Instruct-FP8") == "trtllm"
    assert pick_backend(cfg, "meta-llama/Llama-3.1-70B-NVFP4") == "trtllm"
    assert pick_backend(cfg, "qwen3-235b-fp4") == "trtllm"


def test_packaged_rules_keep_sglang_for_deepseek():
    """Confirm prior SGLang routing wasn't accidentally clobbered."""
    cfg = load_selection()
    assert pick_backend(cfg, "deepseek-v3-671b") == "sglang"
    assert pick_backend(cfg, "deepseek-r1-zero") == "sglang"
