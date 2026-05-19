from __future__ import annotations

from berth.backends.base import ContainerBackend
from berth.lifecycle.plan import DeploymentPlan

# Power-of-two ladder for cuda_graph_config.batch_sizes. trtllm-serve with
# enable_padding rounds each request to the nearest precomputed graph size,
# which keeps cuda-graph dispatch dense across mixed concurrencies.
_BATCH_SIZE_LADDER = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 896)


def _batch_sizes_for(target_concurrency: int) -> list[int]:
    """Build a sorted list covering 1..target_concurrency.

    Includes target itself if not a power of two so the largest precomputed
    graph matches our --max_batch_size exactly.
    """
    sizes = [s for s in _BATCH_SIZE_LADDER if s <= target_concurrency]
    if target_concurrency not in sizes:
        sizes.append(target_concurrency)
    return sorted(sizes)


class TRTLLMBackend(ContainerBackend):
    """NVIDIA TensorRT-LLM via `trtllm-serve` (PyTorch backend).

    Flag naming convention is snake_case (--tp_size, not --tensor-parallel-size).
    The TensorRT-engine path requires an ahead-of-time `trtllm-build` step and
    is not exposed here - use the PyTorch backend for parity with vLLM/SGLang.

    Returns a per-deployment YAML config from `engine_config()` enabling
    `print_iter_log` (so /metrics aggregates engine-level stats - kv-cache,
    batch fill, scheduler) and a CUDA-graph batch-size ladder sized to the
    plan's target_concurrency.

    LoRA adapter hot-load is NOT supported. TRT-LLM's adapter story lives
    on the legacy AOT-engine build path (trtllm-build), which is
    incompatible with the PyTorch-backend deployments we use.
    """

    name = "trtllm"
    supports_adapters = False

    def build_argv(
        self,
        plan: DeploymentPlan,
        *,
        local_model_path: str,
        config_path: str | None = None,
    ) -> list[str]:
        # The nvcr.io/nvidia/tensorrt-llm/release image does not embed the
        # launcher as ENTRYPOINT, so we invoke `trtllm-serve` explicitly.
        # Model path is positional, immediately after the launcher.
        argv: list[str] = [
            "trtllm-serve", local_model_path,
            "--backend", "pytorch",
            "--host", "0.0.0.0",
            "--port", str(self.manifest.internal_port),
            "--tp_size", str(plan.tensor_parallel),
            "--max_seq_len", str(plan.max_model_len),
            "--max_batch_size", str(plan.target_concurrency),
            "--kv_cache_free_gpu_memory_fraction", str(plan.gpu_memory_utilization),
            "--trust_remote_code",
        ]
        if config_path is not None:
            argv.extend(["--config", config_path])
        # TRT-LLM does not accept --served_model_name on the CLI; OpenAI
        # `model` field is matched against the model path/repo. Clients should
        # pass the same string they registered with.
        argv.extend(self.manifest.extra_launch_args)
        self._append_extra(argv, plan.extra_args)
        return argv

    def engine_config(self, plan: DeploymentPlan) -> dict[str, object] | None:
        if plan.target_concurrency is None:
            raise ValueError("target_concurrency must be resolved before TRT-LLM config")
        return {
            # Populates /metrics on the PyTorch backend (TRT-LLM emits as JSON,
            # not Prometheus exposition - our aggregator passes it through but
            # downstream callers must parse it themselves until we add a
            # JSON->Prom translator).
            "enable_iter_perf_stats": True,
            # Mirrors NVIDIA's recommended throughput profile (also writes
            # per-iter logs to stdout for `serve logs` inspection).
            "print_iter_log": True,
            "cuda_graph_config": {
                "enable_padding": True,
                "batch_sizes": _batch_sizes_for(plan.target_concurrency),
            },
        }
