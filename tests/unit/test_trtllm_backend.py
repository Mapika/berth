from berth.backends.trtllm import TRTLLMBackend
from berth.lifecycle.plan import DeploymentPlan


def _plan(**overrides):
    base = dict(
        model_name="llama-1b",
        hf_repo="meta-llama/Llama-3.2-1B-Instruct",
        revision="main",
        backend="trtllm",
        image_tag="nvcr.io/nvidia/tensorrt-llm/release:1.3.0rc14",
        gpu_ids=[0],
        max_model_len=8192,
        target_concurrency=8,
    )
    base.update(overrides)
    return DeploymentPlan(**base)


def test_build_argv_minimum():
    argv = TRTLLMBackend().build_argv(_plan(), local_model_path="/models/llama-1b")
    # trtllm-serve takes the model path positionally, immediately after the
    # launcher. The release image does not embed the launcher, so we prepend it.
    assert argv[0] == "trtllm-serve"
    assert argv[1] == "/models/llama-1b"
    assert "--backend" in argv
    assert argv[argv.index("--backend") + 1] == "pytorch"
    assert "--tp_size" in argv
    assert argv[argv.index("--tp_size") + 1] == "1"
    assert "--max_seq_len" in argv
    assert argv[argv.index("--max_seq_len") + 1] == "8192"
    assert "--max_batch_size" in argv
    assert argv[argv.index("--max_batch_size") + 1] == "8"  # plan default
    assert "--kv_cache_free_gpu_memory_fraction" in argv
    assert "--host" in argv and argv[argv.index("--host") + 1] == "0.0.0.0"
    assert "--port" in argv and argv[argv.index("--port") + 1] == "8000"
    assert "--trust_remote_code" in argv


def test_build_argv_max_batch_size_from_target_concurrency():
    argv = TRTLLMBackend().build_argv(
        _plan(target_concurrency=128), local_model_path="/m",
    )
    assert argv[argv.index("--max_batch_size") + 1] == "128"


def test_build_argv_tp_4():
    argv = TRTLLMBackend().build_argv(
        _plan(gpu_ids=[0, 1, 2, 3], tensor_parallel=4),
        local_model_path="/models/x",
    )
    assert argv[argv.index("--tp_size") + 1] == "4"


def test_container_kwargs_gpu_request():
    kw = TRTLLMBackend().container_kwargs(_plan(gpu_ids=[2, 3], tensor_parallel=2))
    assert kw["device_requests"][0]["device_ids"] == ["2", "3"]
    assert kw["ipc_mode"] == "host"
    assert kw["shm_size"] == "2g"
    assert kw["ulimits"][0].name == "memlock"


def test_default_image():
    assert TRTLLMBackend().image_default.startswith("nvcr.io/nvidia/tensorrt-llm/release:")


def test_internal_port():
    assert TRTLLMBackend().internal_port == 8000


def test_build_argv_extra_args_keyvalue():
    argv = TRTLLMBackend().build_argv(
        _plan(extra_args={"--ep_size": "2", "--max_num_tokens": "8192"}),
        local_model_path="/models/x",
    )
    assert argv[argv.index("--ep_size") + 1] == "2"
    assert argv[argv.index("--max_num_tokens") + 1] == "8192"


def test_build_argv_extra_args_bare_flag():
    argv = TRTLLMBackend().build_argv(
        _plan(extra_args={"--enable-chunked-context": ""}),
        local_model_path="/models/x",
    )
    idx = argv.index("--enable-chunked-context")
    if idx + 1 < len(argv):
        assert argv[idx + 1].startswith("--")
    assert "" not in argv


def test_extra_args_overrides_backend_emission_no_duplicate_flag():
    """Regression: the backend emits --kv_cache_free_gpu_memory_fraction by
    default. If --extra also sets it (e.g. for tuning), the result must be
    a single occurrence with the user's value, not two."""
    argv = TRTLLMBackend().build_argv(
        _plan(extra_args={"--kv_cache_free_gpu_memory_fraction": "0.85"}),
        local_model_path="/models/x",
    )
    occurrences = [i for i, x in enumerate(argv) if x == "--kv_cache_free_gpu_memory_fraction"]
    assert len(occurrences) == 1, f"flag duplicated: {argv}"
    assert argv[occurrences[0] + 1] == "0.85"


def test_engine_config_enables_iter_perf_stats():
    """enable_iter_perf_stats is what populates /metrics on the TRT-LLM
    PyTorch backend; without it the endpoint returns []. Must be on so
    observability works out of the box."""
    cfg = TRTLLMBackend().engine_config(_plan())
    assert cfg is not None
    assert cfg["enable_iter_perf_stats"] is True
    assert cfg["print_iter_log"] is True  # also handy for `berth logs`


def test_engine_config_cuda_graph_batch_sizes_cover_target_concurrency():
    """Padding requires precomputed graphs at every plausible batch size up to
    target_concurrency; otherwise requests above the largest precomputed size
    fall off the cuda-graph fast path."""
    cfg = TRTLLMBackend().engine_config(_plan(target_concurrency=73))
    sizes = cfg["cuda_graph_config"]["batch_sizes"]
    assert sizes == sorted(sizes)
    assert sizes[0] == 1
    assert sizes[-1] == 73, f"largest graph must equal max_batch_size: {sizes}"
    assert cfg["cuda_graph_config"]["enable_padding"] is True


def test_engine_config_batch_sizes_pure_power_of_two_target():
    """When target_concurrency is itself a power of two, no extra duplicate."""
    cfg = TRTLLMBackend().engine_config(_plan(target_concurrency=64))
    assert cfg["cuda_graph_config"]["batch_sizes"] == [1, 2, 4, 8, 16, 32, 64]


def test_build_argv_includes_config_flag_when_path_given():
    """When the manager hands us a config_path, --config <path> appears once."""
    argv = TRTLLMBackend().build_argv(
        _plan(), local_model_path="/models/x",
        config_path="/serve/configs/42.yml",
    )
    i = argv.index("--config")
    assert argv[i + 1] == "/serve/configs/42.yml"
    assert argv.count("--config") == 1


def test_build_argv_omits_config_flag_when_path_none():
    """No --config when manager doesn't pass one (defensive default)."""
    argv = TRTLLMBackend().build_argv(_plan(), local_model_path="/models/x")
    assert "--config" not in argv


def test_supports_adapters_is_false():
    """TRT-LLM PyTorch backend does NOT support adapters; the lifecycle
    enforces this so deployments aren't created with max_loras > 0 against
    a TRT-LLM backend."""
    assert TRTLLMBackend.supports_adapters is False


def test_build_argv_unaware_of_max_loras_does_not_break():
    """TRT-LLM build_argv with max_loras>0 doesn't crash (the higher-level
    create_deployment guard rejects this combo before we reach build_argv;
    test here defends against accidental bypass)."""
    argv = TRTLLMBackend().build_argv(_plan(max_loras=8), local_model_path="/m")
    # No --enable-lora / --max-loras-per-batch / --max-loras emitted by us.
    assert "--enable-lora" not in argv
    assert "--max-loras-per-batch" not in argv
    assert "--max-loras" not in argv
