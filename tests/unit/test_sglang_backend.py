from berth.backends.sglang import SGLangBackend
from berth.lifecycle.plan import DeploymentPlan


def _plan(**overrides):
    base = dict(
        model_name="llama-1b",
        hf_repo="meta-llama/Llama-3.2-1B-Instruct",
        revision="main",
        backend="sglang",
        image_tag="lmsysorg/sglang:v0.5.11",
        gpu_ids=[0],
        max_model_len=8192,
        target_concurrency=8,
    )
    base.update(overrides)
    return DeploymentPlan(**base)


def test_build_argv_minimum():
    argv = SGLangBackend().build_argv(_plan(), local_model_path="/models/llama-1b")
    # The argv prepends the launcher because the SGLang image's ENTRYPOINT
    # is nvidia_entrypoint.sh (no embedded launcher).
    assert argv[0] == "python3"
    assert argv[1] == "-m"
    assert argv[2] == "sglang.launch_server"
    assert "--model-path" in argv
    assert argv[argv.index("--model-path") + 1] == "/models/llama-1b"
    assert "--tp" in argv
    assert argv[argv.index("--tp") + 1] == "1"
    assert "--context-length" in argv
    assert argv[argv.index("--context-length") + 1] == "8192"
    assert "--max-running-requests" in argv
    assert argv[argv.index("--max-running-requests") + 1] == "8"  # plan default
    assert "--mem-fraction-static" in argv
    assert "--host" in argv and argv[argv.index("--host") + 1] == "0.0.0.0"
    assert "--port" in argv and argv[argv.index("--port") + 1] == "30000"


def test_build_argv_max_running_requests_from_target_concurrency():
    argv = SGLangBackend().build_argv(
        _plan(target_concurrency=128), local_model_path="/m",
    )
    assert argv[argv.index("--max-running-requests") + 1] == "128"


def test_build_argv_tp_4():
    argv = SGLangBackend().build_argv(
        _plan(gpu_ids=[0, 1, 2, 3], tensor_parallel=4),
        local_model_path="/models/x",
    )
    assert argv[argv.index("--tp") + 1] == "4"


def test_container_kwargs_gpu_request():
    kw = SGLangBackend().container_kwargs(_plan(gpu_ids=[2, 3], tensor_parallel=2))
    assert kw["device_requests"][0]["device_ids"] == ["2", "3"]
    assert kw["ipc_mode"] == "host"
    assert kw["shm_size"] == "2g"


def test_default_image():
    assert SGLangBackend().image_default.startswith("lmsysorg/sglang:")


def test_internal_port():
    assert SGLangBackend().internal_port == 30000


def test_build_argv_extra_args_keyvalue():
    argv = SGLangBackend().build_argv(
        _plan(extra_args={"--kv-cache-dtype": "fp8_e4m3"}),
        local_model_path="/models/x",
    )
    assert argv[argv.index("--kv-cache-dtype") + 1] == "fp8_e4m3"


def test_build_argv_extra_args_bare_flag():
    argv = SGLangBackend().build_argv(
        _plan(extra_args={"--enable-torch-compile": ""}),
        local_model_path="/models/x",
    )
    idx = argv.index("--enable-torch-compile")
    if idx + 1 < len(argv):
        assert argv[idx + 1].startswith("--")
    assert "" not in argv


def test_manifest_extra_launch_args_appear_in_argv():
    """The version-pinned workaround flags from backends.yaml are forwarded."""
    argv = SGLangBackend().build_argv(_plan(), local_model_path="/models/x")
    assert "--disable-piecewise-cuda-graph" in argv


def test_build_argv_no_lora_flags_when_max_loras_zero():
    """Default behavior unchanged: no LoRA flags emitted when max_loras=0."""
    argv = SGLangBackend().build_argv(_plan(), local_model_path="/m")
    assert "--enable-lora" not in argv
    assert "--max-loras-per-batch" not in argv


def test_build_argv_emits_lora_flags_when_max_loras_set():
    """SGLang uses --max-loras-per-batch (NOT --max-loras like vLLM)."""
    argv = SGLangBackend().build_argv(_plan(max_loras=4), local_model_path="/m")
    assert "--enable-lora" in argv
    j = argv.index("--max-loras-per-batch")
    assert argv[j + 1] == "4"


def test_build_argv_does_not_emit_max_lora_rank_or_target_modules():
    """We do NOT pick max_lora_rank / lora_target_modules ourselves  -
    they're checkpoint-specific. Operators set via --extra if needed."""
    argv = SGLangBackend().build_argv(_plan(max_loras=4), local_model_path="/m")
    assert "--max-lora-rank" not in argv
    assert "--lora-target-modules" not in argv
