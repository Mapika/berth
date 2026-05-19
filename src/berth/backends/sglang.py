from __future__ import annotations

from berth.backends.base import ContainerBackend
from berth.lifecycle.plan import DeploymentPlan


class SGLangBackend(ContainerBackend):
    name = "sglang"
    # SGLang's runtime adapter endpoints sit at root, NOT under /v1/.
    adapter_load_path = "/load_lora_adapter"
    adapter_unload_path = "/unload_lora_adapter"

    def build_argv(
        self,
        plan: DeploymentPlan,
        *,
        local_model_path: str,
        config_path: str | None = None,  # SGLang doesn't use a YAML config file
    ) -> list[str]:
        # The lmsysorg/sglang image's ENTRYPOINT is nvidia_entrypoint.sh which
        # `exec`s its argv. Unlike vLLM's image, it does NOT embed the launcher,
        # so we must include `python3 -m sglang.launch_server` ourselves.
        argv: list[str] = [
            "python3", "-m", "sglang.launch_server",
            "--model-path", local_model_path,
            "--tp", str(plan.tensor_parallel),
            "--context-length", str(plan.max_model_len),
            "--max-running-requests", str(plan.target_concurrency),
            "--mem-fraction-static", str(plan.gpu_memory_utilization),
            "--dtype", plan.dtype,
            "--host", "0.0.0.0",
            "--port", str(self.manifest.internal_port),
            "--served-model-name", plan.model_name,
        ]
        if plan.max_loras > 0:
            # SGLang's slot flag is --max-loras-per-batch (different name
            # from vLLM's --max-loras). For fully dynamic loading without
            # any startup --lora-paths, the SGLang docs recommend setting
            # --max-lora-rank and --lora-target-modules explicitly; we
            # don't pick those here because they're checkpoint-specific.
            # Operators that need them pass --extra "--max-lora-rank=64"
            # and --extra "--lora-target-modules=all".
            argv.extend([
                "--enable-lora",
                "--max-loras-per-batch", str(plan.max_loras),
            ])
        argv.extend(self.manifest.extra_launch_args)
        self._append_extra(argv, plan.extra_args)
        return argv
