from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

from docker.errors import NotFound  # type: ignore[import-untyped]

import docker  # type: ignore[import-untyped]

log = logging.getLogger(__name__)


class ImageDigestMismatch(RuntimeError):
    """Raised when a running container's image does not match the
    operator-pinned `pinned_digest`. Refuses to mark the deployment ready."""


@dataclass(frozen=True)
class ContainerHandle:
    id: str
    name: str
    address: str       # host or container name reachable from daemon
    port: int          # port on `address` to talk HTTP to


class DockerClient:
    def __init__(self, *, client: Any | None = None, network_name: str):
        self._client: Any = client if client is not None else cast(Any, docker).from_env()
        self._network_name = network_name

    def ensure_network(self) -> None:
        try:
            self._client.networks.get(self._network_name)
        except NotFound:
            log.info("creating docker network %s", self._network_name)
            self._client.networks.create(self._network_name, driver="bridge")

    def run(
        self,
        *,
        image: str,
        name: str,
        command: list[str],
        environment: dict[str, str],
        kwargs: dict[str, object],
        volumes: dict[str, dict[str, str]],
        internal_port: int,
    ) -> ContainerHandle:
        container = self._client.containers.run(
            image=image,
            command=command,
            name=name,
            environment=environment,
            volumes=volumes,
            network=self._network_name,
            ports={f"{internal_port}/tcp": ("127.0.0.1", None)},
            detach=True,
            **kwargs,
        )
        # Reload to pick up port allocation
        container.reload()
        port_bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        binding = port_bindings.get(f"{internal_port}/tcp")
        if not binding:
            # Container started but the port mapping isn't reported yet; this is rare
            # but defensible - fall back to the internal port on the container name
            # (only works if daemon is on the same docker network; documented limitation).
            host_port = internal_port
            address = name
        else:
            host_port = int(binding[0]["HostPort"])
            address = "127.0.0.1"
        return ContainerHandle(
            id=container.id,
            name=name,
            address=address,
            port=host_port,
        )

    def stop(
        self,
        container_id: str,
        *,
        timeout: int = 30,
        remove: bool = True,
    ) -> None:
        """Stop a container. Removes it by default; pass remove=False to keep
        the stopped container around (e.g. so failed-load engine logs survive
        for `docker logs <name>` inspection).
        """
        try:
            c = self._client.containers.get(container_id)
        except NotFound:
            return
        c.stop(timeout=timeout)
        if remove:
            c.remove()

    def remove(self, container_id: str, *, force: bool = False) -> None:
        """Remove a container if it exists."""
        try:
            c = self._client.containers.get(container_id)
        except NotFound:
            return
        c.remove(force=force)

    def container_status(self, container_id: str) -> str | None:
        """Return the container's status string ("running", "exited",
        "created", ...) or None if it no longer exists. Used by
        reconcile to detect orphaned rows without reaching into
        `self._client` directly."""
        try:
            c = self._client.containers.get(container_id)
        except NotFound:
            return None
        return c.status

    def container_image_id(self, container_id: str) -> str | None:
        """Return the content-addressable id (`sha256:...`) of the image the
        container was started from. The docker SDK exposes this on
        `container.image.id`; it is always populated for pulled or
        locally-built images. `RepoDigests` is intentionally NOT used:
        it is empty for locally-built images and unreliable across registry
        re-pulls. Returns None if the container is gone.
        """
        try:
            c = self._client.containers.get(container_id)
        except NotFound:
            return None
        image = getattr(c, "image", None)
        if image is None:
            return None
        return getattr(image, "id", None)

    def verify_image_digest(self, container_id: str, pinned_digest: str) -> None:
        """Verify the running container's image matches the operator-pinned
        content-addressable digest, raising `ImageDigestMismatch` on mismatch.

        Tags (``vllm/vllm-openai:vX.Y.Z``) are mutable: upstream can retag the
        same name to a different image. When a backend pins a `pinned_digest`
        (`sha256:...`) in backends.yaml, we refuse to mark the deployment ready
        unless the image actually launched matches that digest.

        The container's image id is taken from `container.image.id`
        (see `container_image_id`); `RepoDigests` are also accepted so an
        operator may pin either the local image id or a registry repo-digest.
        A missing container or unresolvable image id is itself a mismatch -
        we cannot prove the running image is the pinned one, so we refuse.
        """
        actual_id = self.container_image_id(container_id)
        candidates: set[str] = set()
        if actual_id:
            candidates.add(actual_id)
        # Also accept registry repo-digests when present (e.g. when the operator
        # pinned the digest as published by the registry rather than the local id).
        try:
            c = self._client.containers.get(container_id)
            image = getattr(c, "image", None)
            repo_digests = (getattr(image, "attrs", {}) or {}).get("RepoDigests") or []
            for rd in repo_digests:
                # RepoDigests look like "repo@sha256:..."; pin may be the bare digest.
                if "@" in rd:
                    candidates.add(rd.split("@", 1)[1])
                candidates.add(rd)
        except NotFound:
            pass
        if pinned_digest not in candidates:
            raise ImageDigestMismatch(
                f"image digest mismatch for container {container_id}: "
                f"pinned {pinned_digest!r} but running image is "
                f"{actual_id!r} (refusing to mark ready)"
            )

    def container_pids(self, container_id: str) -> list[int]:
        """All host-side PIDs running inside the container, including
        children spawned by the entrypoint (e.g. vLLM EngineCore subprocs).
        Empty list if the container is gone or not running.
        """
        try:
            c = self._client.containers.get(container_id)
            top = c.top()  # docker top: host-pid view
        except Exception:
            return []
        titles = top.get("Titles") or []
        rows = top.get("Processes") or []
        try:
            pid_idx = titles.index("PID")
        except ValueError:
            pid_idx = 1  # convention: ['UID', 'PID', 'PPID', ...]
        out: list[int] = []
        for row in rows:
            if pid_idx < len(row):
                try:
                    out.append(int(row[pid_idx]))
                except ValueError:
                    pass
        return out

    def stream_logs(
        self,
        container_id: str,
        *,
        follow: bool = False,
        tail: int | str = "all",
    ) -> Iterator[bytes]:
        c = self._client.containers.get(container_id)
        return c.logs(stream=True, follow=follow, tail=tail)

    def pull(self, image: str) -> None:
        self._client.images.pull(image)
