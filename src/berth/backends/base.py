from __future__ import annotations

from typing import ClassVar, Protocol

from docker.types import Ulimit  # type: ignore[import-untyped]

from berth.backends.manifest import EngineManifest, Headroom, load_manifest
from berth.lifecycle.plan import DeploymentPlan


class Backend(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def supports_adapters(self) -> bool: ...
    @property
    def adapter_load_path(self) -> str: ...
    @property
    def adapter_unload_path(self) -> str: ...

    @property
    def image_default(self) -> str: ...
    @property
    def health_path(self) -> str: ...
    @property
    def openai_base(self) -> str: ...
    @property
    def metrics_path(self) -> str: ...
    @property
    def internal_port(self) -> int: ...
    @property
    def headroom(self) -> Headroom: ...

    def build_argv(
        self, plan: DeploymentPlan, *, local_model_path: str, config_path: str | None = None,
    ) -> list[str]: ...
    def container_env(self, plan: DeploymentPlan) -> dict[str, str]: ...
    def container_kwargs(self, plan: DeploymentPlan) -> dict[str, object]: ...
    def engine_config(self, plan: DeploymentPlan) -> dict[str, object] | None: ...


class ContainerBackend:
    """Shared scaffolding for engines that run as Docker containers with
    NVIDIA device requests. Subclasses set `name` and implement `build_argv`."""

    name: ClassVar[str]
    # True if this backend can hot-load LoRA adapters at runtime via the
    # engine's load/unload HTTP endpoints. vLLM and SGLang inherit True;
    # TRT-LLM overrides to False (its adapter story is on the older
    # AOT-engine path, incompatible with our PyTorch-backend deployments).
    supports_adapters: ClassVar[bool] = True
    # Per-engine HTTP paths for dynamic adapter load/unload. vLLM uses
    # /v1/* paths; SGLang uses bare paths. Override per backend.
    adapter_load_path: ClassVar[str] = "/v1/load_lora_adapter"
    adapter_unload_path: ClassVar[str] = "/v1/unload_lora_adapter"
    def __init__(self, manifest: EngineManifest | None = None):
        if manifest is None:
            manifest = load_manifest()[self.name]
        self.manifest = manifest

    @property
    def image_default(self) -> str:
        return self.manifest.image_default

    @property
    def health_path(self) -> str:
        return self.manifest.health_path

    @property
    def openai_base(self) -> str:
        return self.manifest.openai_base

    @property
    def metrics_path(self) -> str:
        return self.manifest.metrics_path

    @property
    def internal_port(self) -> int:
        return self.manifest.internal_port

    @property
    def headroom(self) -> Headroom:
        return self.manifest.headroom

    def build_argv(
        self,
        plan: DeploymentPlan,
        *,
        local_model_path: str,
        config_path: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def engine_config(self, plan: DeploymentPlan) -> dict[str, object] | None:
        """Optional per-deployment YAML config for the engine.

        Backends that support a `--config <file>` flag (or equivalent) can
        return a dict here; the manager serializes it to YAML, mounts it
        into the container, and passes the in-container path to build_argv
        as `config_path=`. Returning None means no config file is needed.
        """
        return None

    def container_env(self, plan: DeploymentPlan) -> dict[str, str]:
        return {}

    def container_kwargs(self, plan: DeploymentPlan) -> dict[str, object]:
        return {
            "device_requests": [
                {
                    "Driver": "nvidia",
                    "device_ids": [str(g) for g in plan.gpu_ids],
                    "Capabilities": [["gpu"]],
                }
            ],
            "ipc_mode": "host",
            "shm_size": "2g",
            "ulimits": [Ulimit(name="memlock", soft=-1, hard=-1)],
        }

    @staticmethod
    def _append_extra(argv: list[str], extra: dict[str, str]) -> None:
        """Append user-provided extra_args from the deployment plan.

        Empty value means bare flag (e.g. --enable-expert-parallel).

        If the backend already emitted the same flag, the earlier emission
        (and its value) is removed first so the user override wins cleanly
        instead of relying on argparse last-value-wins semantics - strict
        argparsers (some TRT-LLM versions) reject duplicate flags outright.
        Heuristic for "next token is the flag's value": doesn't start with
        '--'. Engine flag values are paths/numbers/dtype names; collisions
        are not a real concern.
        """
        for k, v in extra.items():
            i = 0
            while i < len(argv):
                if argv[i] == k:
                    if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                        del argv[i:i + 2]
                    else:
                        del argv[i]
                else:
                    i += 1
            if v == "":
                argv.append(k)
            else:
                argv.extend([k, v])
