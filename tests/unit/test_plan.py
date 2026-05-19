import pytest

from berth.lifecycle.plan import DeploymentPlan


def test_plan_basic_fields():
    p = DeploymentPlan(
        model_name="llama-1b",
        hf_repo="meta-llama/Llama-3.2-1B-Instruct",
        revision="main",
        backend="vllm",
        image_tag="vllm/vllm-openai:v0.7.3",
        gpu_ids=[0],
        max_model_len=8192,
    )
    assert p.tensor_parallel == 1
    assert p.dtype == "auto"
    assert p.gpu_memory_utilization == 0.9


def test_plan_tensor_parallel_must_match_gpu_count():
    with pytest.raises(ValueError, match="tensor_parallel"):
        DeploymentPlan(
            model_name="x",
            hf_repo="org/x",
            revision="main",
            backend="vllm",
            image_tag="vllm/vllm-openai:v0.7.3",
            gpu_ids=[0, 1],
            tensor_parallel=4,
            max_model_len=8192,
        )


def test_plan_tensor_parallel_must_be_power_of_two():
    with pytest.raises(ValueError, match="power of 2"):
        DeploymentPlan(
            model_name="x",
            hf_repo="org/x",
            revision="main",
            backend="vllm",
            image_tag="img:v1",
            gpu_ids=[0, 1, 2],
            tensor_parallel=3,
            max_model_len=8192,
        )


def test_plan_backend_must_be_supported():
    with pytest.raises(ValueError, match="backend"):
        DeploymentPlan(
            model_name="x",
            hf_repo="org/x",
            revision="main",
            backend="trt-llm",  # not in plan 01
            image_tag="img:v1",
            gpu_ids=[0],
            max_model_len=8192,
        )
