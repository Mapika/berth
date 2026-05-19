from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

ACTIVATION_OVERHEAD = 1.15


@dataclass(frozen=True)
class KVEstimateInput:
    model_dir: Path
    max_model_len: int
    target_concurrency: int
    dtype: str


def _dtype_bytes(dtype: str, torch_dtype: str | None) -> int:
    if dtype == "fp8":
        return 1
    if dtype in ("fp16", "bf16"):
        return 2
    if dtype == "auto":
        if torch_dtype in ("float16", "bfloat16"):
            return 2
        if torch_dtype == "float32":
            return 4
        return 2  # default
    return 2


def read_model_config(model_dir: Path) -> dict:
    p = Path(model_dir) / "config.json"
    if not p.exists():
        raise FileNotFoundError(f"no config.json under {model_dir}")
    return json.loads(p.read_text())


def _arch_config(cfg: dict) -> dict:
    """Multimodal HF configs nest the LM architecture under text_config."""
    return cfg.get("text_config") or cfg


def _weight_dtype_bytes(cfg: dict, dtype: str) -> int:
    """Bytes-per-weight, taking checkpoint quantization into account.

    The user-facing `dtype` flag describes compute precision; weights on disk
    may be quantized lower. For sizing we want the on-disk footprint, so an
    fp8-quantized checkpoint reports 1 byte regardless of the compute dtype.
    """
    qcfg = cfg.get("quantization_config") or {}
    qmethod = (qcfg.get("quant_method") or "").lower()
    if qmethod in ("fp8", "nvfp4", "fp4"):
        return 1
    text = _arch_config(cfg)
    return _dtype_bytes(dtype, text.get("torch_dtype") or text.get("dtype"))


def _estimate_param_bytes(cfg: dict, dtype_bytes: int) -> int:
    """Rough parameter count from config; used when safetensors metadata unavailable."""
    text = _arch_config(cfg)
    L = int(text.get("num_hidden_layers", 0))
    H = int(text.get("hidden_size", 0))
    vocab = int(text.get("vocab_size") or cfg.get("vocab_size") or 0)
    if L == 0 or H == 0:
        return 0
    n_heads = int(text.get("num_attention_heads", 1))
    n_kv_heads = int(text.get("num_key_value_heads", n_heads))
    head_dim = int(text.get("head_dim", H // n_heads if n_heads else 0))

    # Attention (Q/K/V/O) - GQA-aware.
    q_proj = H * n_heads * head_dim
    kv_proj = H * n_kv_heads * head_dim * 2  # K and V
    o_proj = n_heads * head_dim * H
    attn_per_layer = q_proj + kv_proj + o_proj

    # FFN: MoE replaces the dense FFN with N experts (+ optional shared).
    n_experts = int(text.get("num_experts") or text.get("num_local_experts") or 0)
    moe_inter = int(text.get("moe_intermediate_size") or 0)
    shared_inter = int(text.get("shared_expert_intermediate_size") or 0)
    if n_experts and moe_inter:
        # 3 matrices (gate/up/down) per expert, each H x moe_inter.
        ffn_per_layer = 3 * H * (n_experts * moe_inter + shared_inter)
    else:
        inter = int(text.get("intermediate_size", 4 * H))
        ffn_per_layer = 3 * H * inter

    embed = vocab * H * 2  # input + output embeddings
    return (L * (attn_per_layer + ffn_per_layer) + embed) * dtype_bytes


def _count_attention_layers(cfg: dict) -> int:
    """Count layers that hold a per-token KV cache.

    Hybrid models (Qwen3.6, Granite-Hybrid, etc.) list per-layer types as
    "full_attention"/"linear_attention" under text_config.layer_types - only
    full-attention layers cache K/V per token; linear-attention layers hold a
    fixed-size state cache that doesn't scale with sequence length.
    """
    text = _arch_config(cfg)
    layer_types = text.get("layer_types")
    n_layers = int(text.get("num_hidden_layers", 0))
    if not layer_types:
        return n_layers
    return sum(1 for t in layer_types if "linear" not in str(t).lower())


def default_target_concurrency(
    model_dir: Path,
    max_model_len: int,
    dtype: str,
    *,
    kv_budget_mb: int = 16384,
    floor: int = 8,
    cap: int = 256,
) -> int:
    """Pick a target_concurrency that fits within ~`kv_budget_mb` of KV cache.

    The static fallback of 8 is right for ~30B-class models and ~16x too low
    for sub-1B models. This computes per-token KV bytes from the model config
    (architecture-aware: GQA, hybrid layers, fp8) and divides the budget by
    the per-request KV footprint.

    `kv_budget_mb` is a target, not a hard cap - placement+headroom downstream
    enforce VRAM limits. Default 16 GB matches a comfortable single-deployment
    KV slice on workstation Blackwell / H100 PCIe; bump for dedicated H100 SXM.

    Falls back to `floor` if the model config can't be read or is malformed  -
    the downstream estimator will still try to read it and will raise the
    actual error to the caller; this function should never block a load on
    its own.
    """
    try:
        cfg = read_model_config(model_dir)
    except (FileNotFoundError, ValueError):
        return floor
    text = _arch_config(cfg)
    n_heads = int(text.get("num_attention_heads", 1))
    n_kv_heads = int(text.get("num_key_value_heads", n_heads))
    head_dim = int(text.get("head_dim", text.get("hidden_size", 0) // n_heads if n_heads else 0))
    n_attn_layers = _count_attention_layers(cfg)
    kv_bytes_per_elem = _dtype_bytes(dtype, text.get("torch_dtype") or text.get("dtype"))
    kv_bytes_per_token = 2 * n_attn_layers * n_kv_heads * head_dim * kv_bytes_per_elem
    if kv_bytes_per_token == 0:
        return floor
    kv_bytes_per_request = kv_bytes_per_token * max_model_len
    if kv_bytes_per_request == 0:
        return floor
    concurrency = (kv_budget_mb * 1024 * 1024) // kv_bytes_per_request
    return max(floor, min(cap, int(concurrency)))


def estimate_vram_mb(inp: KVEstimateInput) -> int:
    cfg = read_model_config(inp.model_dir)
    text = _arch_config(cfg)

    weight_bytes = _weight_dtype_bytes(cfg, inp.dtype)
    kv_bytes_per_elem = _dtype_bytes(
        inp.dtype, text.get("torch_dtype") or text.get("dtype"),
    )

    hidden = int(text.get("hidden_size", 0))
    n_heads = int(text.get("num_attention_heads", 1))
    n_kv_heads = int(text.get("num_key_value_heads", n_heads))
    head_dim = int(text.get("head_dim", hidden // n_heads if n_heads else 0))

    n_attn_layers = _count_attention_layers(cfg)
    kv_bytes_per_token = 2 * n_attn_layers * n_kv_heads * head_dim * kv_bytes_per_elem
    kv_bytes = kv_bytes_per_token * inp.max_model_len * inp.target_concurrency
    weights_bytes = _estimate_param_bytes(cfg, weight_bytes)

    total = (weights_bytes + kv_bytes) * ACTIVATION_OVERHEAD
    return math.ceil(total / 1024 / 1024)
