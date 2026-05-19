from unittest.mock import MagicMock, patch

from berth.observability.gpu_stats import read_gpu_stats


@patch("berth.observability.gpu_stats.pynvml")
def test_read_gpu_stats(mock_nvml):
    mock_nvml.nvmlInit = MagicMock()
    mock_nvml.nvmlDeviceGetCount.return_value = 1
    handle = MagicMock()
    mock_nvml.nvmlDeviceGetHandleByIndex.return_value = handle
    mock_nvml.nvmlDeviceGetMemoryInfo.return_value = MagicMock(
        used=20 * 1024**3, total=80 * 1024**3,
    )
    util = MagicMock()
    util.gpu = 42
    mock_nvml.nvmlDeviceGetUtilizationRates.return_value = util
    mock_nvml.nvmlDeviceGetPowerUsage.return_value = 350_000  # mW

    snaps = read_gpu_stats()
    assert len(snaps) == 1
    s = snaps[0]
    assert s.index == 0
    assert s.memory_used_mb == 20 * 1024
    assert s.memory_total_mb == 80 * 1024
    assert s.gpu_util_pct == 42
    assert s.power_w == 350


@patch("berth.observability.gpu_stats.pynvml", None)
def test_read_gpu_stats_without_pynvml():
    assert read_gpu_stats() == []
