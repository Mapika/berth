from unittest.mock import MagicMock, patch

from berth.lifecycle.topology import read_topology, reset_cache


@patch("berth.lifecycle.topology.pynvml")
def test_read_topology_basic(mock_nvml):
    reset_cache()
    mock_nvml.nvmlInit = MagicMock()
    mock_nvml.nvmlDeviceGetCount.return_value = 2
    devs = [MagicMock(), MagicMock()]
    mock_nvml.nvmlDeviceGetHandleByIndex.side_effect = devs * 10  # called repeatedly
    mock_nvml.nvmlDeviceGetName.side_effect = [b"H100", b"H100"]
    mock_nvml.nvmlDeviceGetMemoryInfo.side_effect = [
        MagicMock(total=80 * 1024**3),
        MagicMock(total=80 * 1024**3),
    ]
    mock_nvml.NVML_TOPOLOGY_NVLINK = 1
    mock_nvml.nvmlDeviceGetTopologyCommonAncestor.return_value = 1  # = NVLINK

    topo = read_topology()
    assert len(topo.gpus) == 2
    assert topo.gpus[0].total_mb == 80 * 1024
    # Both GPUs in the same NVLink island
    assert topo.nvlink_island(0) == frozenset({0, 1})
    assert topo.nvlink_island(1) == frozenset({0, 1})


@patch("berth.lifecycle.topology.pynvml")
def test_read_topology_no_nvlink(mock_nvml):
    reset_cache()
    mock_nvml.nvmlInit = MagicMock()
    mock_nvml.nvmlDeviceGetCount.return_value = 1
    mock_nvml.nvmlDeviceGetHandleByIndex.return_value = MagicMock()
    mock_nvml.nvmlDeviceGetName.return_value = b"A100"
    mock_nvml.nvmlDeviceGetMemoryInfo.return_value = MagicMock(total=40 * 1024**3)

    topo = read_topology()
    assert len(topo.gpus) == 1
    assert topo.nvlink_island(0) == frozenset({0})


@patch("berth.lifecycle.topology.pynvml", None)
def test_read_topology_no_pynvml():
    reset_cache()
    topo = read_topology()
    assert topo.gpus == []


@patch("berth.lifecycle.topology.pynvml")
def test_read_topology_nvml_init_failure_returns_empty_topology(mock_nvml):
    reset_cache()
    mock_nvml.nvmlInit.side_effect = RuntimeError("NVML unavailable")

    topo = read_topology()

    assert topo.gpus == []
