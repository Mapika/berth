from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SUPPORTED_BACKENDS = ("vllm", "sglang", "trtllm")
SUPPORTED_DTYPES = ("auto", "bf16", "fp16", "fp8")
BackendName = Literal["vllm", "sglang", "trtllm"]


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


@dataclass(frozen=True)
class DeploymentPlan:
    model_name: str
    hf_repo: str
    revision: str
    backend: BackendName
    image_tag: str
    gpu_ids: list[int]
    max_model_len: int
    tensor_parallel: int = 1
    dtype: str = "auto"
    gpu_memory_utilization: float = 0.9
    enable_prefix_caching: bool = True
    enable_chunked_prefill: bool = True
    extra_args: dict[str, str] = field(default_factory=dict)
    pinned: bool = False
    idle_timeout_s: int | None = None
    # Target concurrent decode streams. Drives both the KV estimator's VRAM
    # reservation and the engine's --max-num-seqs / --max-running-requests /
    # --max_batch_size. None = let the manager pick a model-size-aware value
    # at load time (see kv_estimator.default_target_concurrency).
    target_concurrency: int | None = None
    # LoRA adapter slot count for this deployment. 0 = LoRA disabled.
    # If >0, the chosen backend must have supports_adapters=True (validated
    # by the manager at deployment-creation time, since DeploymentPlan
    # itself doesn't know about backend objects). Adapters loaded into this
    # deployment are tracked in the deployment_adapters junction table.
    max_loras: int = 0
    # Max per-adapter LoRA rank this deployment supports - comes from the
    # operator's `-x '--max-lora-rank=N'`. 0 = unset; the runtime treats
    # 0 as the engine default (16 for vLLM/SGLang). The value is stored
    # on the deployment row so ensure_adapter_loaded can pre-flight an
    # adapter's rank against this limit instead of letting the engine
    # error out cryptically on first hot-load.
    max_lora_rank: int = 0
    # Target node for this deployment. None / "local" → runs on the leader
    # host via the in-process docker client. Any other label resolves to
    # an enrolled agent's node_id; the manager dispatches via that node's
    # AgentLink and the engine container lives on that host.
    node_label: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in SUPPORTED_BACKENDS:
            raise ValueError(
                f"backend {self.backend!r} not supported "
                f"(supported: {SUPPORTED_BACKENDS})"
            )
        if self.dtype not in SUPPORTED_DTYPES:
            raise ValueError(f"dtype {self.dtype!r} not in {SUPPORTED_DTYPES}")
        if self.tensor_parallel < 1:
            raise ValueError("tensor_parallel must be >= 1")
        if not _is_power_of_two(self.tensor_parallel):
            raise ValueError("tensor_parallel must be a power of 2")
        if self.tensor_parallel != len(self.gpu_ids):
            raise ValueError(
                "tensor_parallel must equal len(gpu_ids); "
                f"got TP={self.tensor_parallel}, gpus={self.gpu_ids}"
            )
        if not 0.05 <= self.gpu_memory_utilization <= 1.0:
            raise ValueError("gpu_memory_utilization must be in [0.05, 1.0]")
        if self.max_loras < 0:
            raise ValueError("max_loras must be >= 0")
        if self.max_lora_rank < 0:
            raise ValueError("max_lora_rank must be >= 0")
