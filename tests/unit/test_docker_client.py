from unittest.mock import MagicMock

import pytest

from berth.lifecycle.docker_client import ContainerHandle, DockerClient


@pytest.fixture
def fake_docker():
    """A MagicMock that imitates the bits of docker.from_env() we touch."""
    client = MagicMock()
    container = MagicMock()
    container.id = "abc123"
    container.name = "vllm-llama-1b"
    container.attrs = {
        "NetworkSettings": {
            "Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49152"}]},
            "Networks": {"berth-engines": {"IPAddress": "172.20.0.5"}},
        }
    }
    container.reload = MagicMock()  # no-op; attrs is already populated above
    client.containers.run.return_value = container
    return client


def test_run_container_returns_handle(fake_docker):
    dc = DockerClient(client=fake_docker, network_name="berth-engines")
    handle = dc.run(
        image="vllm/vllm-openai:v0.7.3",
        name="vllm-llama-1b",
        command=["--model", "/models/x"],
        environment={},
        kwargs={"ipc_mode": "host"},
        volumes={"/host/models": {"bind": "/models", "mode": "ro"}},
        internal_port=8000,
    )
    assert isinstance(handle, ContainerHandle)
    assert handle.id == "abc123"
    assert handle.address == "127.0.0.1"  # bound to localhost
    assert handle.port == 49152            # allocated host port
    # Verify ports kwarg was passed
    call_kwargs = fake_docker.containers.run.call_args.kwargs
    assert call_kwargs["ports"] == {"8000/tcp": ("127.0.0.1", None)}


def test_run_creates_network_if_missing(fake_docker):
    from docker.errors import NotFound
    fake_docker.networks.get.side_effect = NotFound("not found")
    dc = DockerClient(client=fake_docker, network_name="berth-engines")
    dc.ensure_network()
    fake_docker.networks.create.assert_called_once_with("berth-engines", driver="bridge")


def test_ensure_network_propagates_other_errors(fake_docker):
    fake_docker.networks.get.side_effect = RuntimeError("daemon connection refused")
    dc = DockerClient(client=fake_docker, network_name="berth-engines")
    with pytest.raises(RuntimeError, match="daemon connection refused"):
        dc.ensure_network()


def test_run_skips_network_create_if_present(fake_docker):
    dc = DockerClient(client=fake_docker, network_name="berth-engines")
    dc.ensure_network()
    fake_docker.networks.create.assert_not_called()


def test_stop_calls_remove(fake_docker):
    container = MagicMock()
    fake_docker.containers.get.return_value = container
    dc = DockerClient(client=fake_docker, network_name="berth-engines")
    dc.stop("abc123", timeout=10)
    container.stop.assert_called_once_with(timeout=10)
    container.remove.assert_called_once()


def test_stop_is_idempotent_for_missing_container(fake_docker):
    from docker.errors import NotFound
    fake_docker.containers.get.side_effect = NotFound("gone")
    dc = DockerClient(client=fake_docker, network_name="berth-engines")
    dc.stop("abc123", timeout=10)  # must not raise


def test_stop_preserves_container_when_remove_false(fake_docker):
    """Used by the failed-load path so engine logs survive for inspection."""
    container = MagicMock()
    fake_docker.containers.get.return_value = container
    dc = DockerClient(client=fake_docker, network_name="berth-engines")
    dc.stop("abc123", timeout=10, remove=False)
    container.stop.assert_called_once_with(timeout=10)
    container.remove.assert_not_called()


def test_container_pids_returns_all_host_pids(fake_docker):
    """`docker top` row -> list of int PIDs from the PID column."""
    container = MagicMock()
    container.top.return_value = {
        "Titles": ["UID", "PID", "PPID", "C", "STIME", "TTY", "TIME", "CMD"],
        "Processes": [
            ["root", "12345", "12300", "0", "10:00", "?", "00:00:00", "python -m"],
            ["root", "12346", "12345", "0", "10:00", "?", "00:00:00", "engine_core"],
        ],
    }
    fake_docker.containers.get.return_value = container
    dc = DockerClient(client=fake_docker, network_name="berth-engines")
    assert dc.container_pids("abc") == [12345, 12346]


def test_container_pids_handles_missing_container(fake_docker):
    from docker.errors import NotFound
    fake_docker.containers.get.side_effect = NotFound("gone")
    dc = DockerClient(client=fake_docker, network_name="berth-engines")
    assert dc.container_pids("abc") == []
