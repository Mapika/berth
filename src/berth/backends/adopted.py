"""Minimal backend sentinel for adopted (externally-hosted) deployments.

Adopted deployments run on their own engine (vLLM, SGLang, TRT-LLM, or any
OpenAI-compatible server) that berth did NOT launch. The leader proxy reads
`backend.openai_base` to build the upstream URL. All other Backend Protocol
members are irrelevant for adopted rows:

- No Docker image is ever pulled or started for an adopted deployment.
- Adapters cannot be hot-loaded into an adopted deployment (adapter_name
  is always None for adopted rows, so ensure_adapter_loaded is never called).
- Health checks for adopted deployments are handled by the agent, not by
  berth's HealthMonitor (which skips source='adopted' rows, as do the
  reaper and manager._stop_locked).

The sentinel raises NotImplementedError on all non-routing accessors so any
accidental call site is immediately obvious rather than silently wrong.
"""
from __future__ import annotations

from berth.lifecycle.plan import DeploymentPlan


class AdoptedBackend:
    """Sentinel backend registered as 'adopted' in the leader's backends dict.

    Exists solely so that `backends.get('adopted')` returns a non-None object
    whose `.openai_base` the proxy can read. Nothing else on this class is
    called for adopted deployments in the normal proxy path.
    """

    name: str = "adopted"
    supports_adapters: bool = False
    adapter_load_path: str = ""
    adapter_unload_path: str = ""

    @property
    def openai_base(self) -> str:
        return "/v1"

    # The remaining Backend Protocol members are sentinel stubs. Adopted
    # deployments never go through the launch / health / metrics path, so
    # these should never be called. Raising makes any accidental call site
    # immediately visible instead of silently returning wrong data.

    @property
    def image_default(self) -> str:
        raise NotImplementedError("AdoptedBackend has no image (never launched by berth)")

    @property
    def health_path(self) -> str:
        raise NotImplementedError("AdoptedBackend health is managed by the reporting agent")

    @property
    def metrics_path(self) -> str:
        raise NotImplementedError("AdoptedBackend has no berth-managed metrics path")

    @property
    def internal_port(self) -> int:
        raise NotImplementedError("AdoptedBackend has no fixed internal port")

    @property
    def headroom(self):
        raise NotImplementedError("AdoptedBackend has no VRAM headroom model")

    def build_argv(
        self,
        plan: DeploymentPlan,
        *,
        local_model_path: str,
        config_path: str | None = None,
    ) -> list[str]:
        raise NotImplementedError("AdoptedBackend cannot build launch argv")

    def container_env(self, plan: DeploymentPlan) -> dict[str, str]:
        raise NotImplementedError("AdoptedBackend has no container env")

    def container_kwargs(self, plan: DeploymentPlan) -> dict[str, object]:
        raise NotImplementedError("AdoptedBackend has no container kwargs")

    def engine_config(self, plan: DeploymentPlan) -> dict[str, object] | None:
        raise NotImplementedError("AdoptedBackend has no engine config")
