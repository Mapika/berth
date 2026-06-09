"""Tests for the engine-image security fixes:

FINDING 5 - engine containers must not default to host IPC namespace.
FINDING 4 - optional digest-pin verification of the launched image.
"""
from unittest.mock import MagicMock

import pytest

from berth.backends.vllm import VLLMBackend
from berth.lifecycle.docker_client import DockerClient, ImageDigestMismatch
from berth.lifecycle.plan import DeploymentPlan


def _plan(**overrides):
    base = dict(
        model_name="llama-1b",
        hf_repo="meta-llama/Llama-3.2-1B-Instruct",
        revision="main",
        backend="vllm",
        image_tag="vllm/vllm-openai:v0.7.3",
        gpu_ids=[0],
        max_model_len=8192,
        target_concurrency=8,
    )
    base.update(overrides)
    return DeploymentPlan(**base)


# --- FINDING 5: no host IPC by default ---------------------------------------

def test_default_container_kwargs_not_host_ipc():
    kw = VLLMBackend().container_kwargs(_plan())
    assert kw.get("ipc_mode") != "host"
    # The private shm_size still covers the single-container case.
    assert kw["shm_size"] == "2g"


# --- FINDING 4: digest-pin verification --------------------------------------

def _client_with_image(image_id, repo_digests=None):
    client = MagicMock()
    container = MagicMock()
    container.id = "abc123"
    image = MagicMock()
    image.id = image_id
    image.attrs = {"RepoDigests": repo_digests or []}
    container.image = image
    client.containers.get.return_value = container
    return client


def test_verify_image_digest_passes_on_match():
    digest = "sha256:" + "a" * 64
    dc = DockerClient(client=_client_with_image(digest), network_name="berth-engines")
    # Must not raise when the running image id matches the pinned digest.
    dc.verify_image_digest("abc123", digest)


def test_verify_image_digest_raises_on_mismatch():
    running = "sha256:" + "a" * 64
    pinned = "sha256:" + "b" * 64
    dc = DockerClient(client=_client_with_image(running), network_name="berth-engines")
    with pytest.raises(ImageDigestMismatch, match="digest mismatch"):
        dc.verify_image_digest("abc123", pinned)


def test_verify_image_digest_accepts_repo_digest():
    pinned = "sha256:" + "c" * 64
    repo = f"vllm/vllm-openai@{pinned}"
    dc = DockerClient(
        client=_client_with_image("sha256:" + "9" * 64, repo_digests=[repo]),
        network_name="berth-engines",
    )
    # Operator pinned the registry repo-digest rather than the local image id.
    dc.verify_image_digest("abc123", pinned)


def test_verify_image_digest_raises_when_container_gone():
    from docker.errors import NotFound

    client = MagicMock()
    client.containers.get.side_effect = NotFound("gone")
    dc = DockerClient(client=client, network_name="berth-engines")
    with pytest.raises(ImageDigestMismatch):
        dc.verify_image_digest("abc123", "sha256:" + "d" * 64)
