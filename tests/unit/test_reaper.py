import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from berth.lifecycle.reaper import Reaper


@pytest.mark.asyncio
async def test_reaper_evicts_idle():
    now = 1_000_000
    deployments = [
        # idle 600s; default timeout 300 -> evict
        MagicMock(id=1, pinned=False, idle_timeout_s=None,
                  last_request_at=now - 600, status="ready"),
        # idle 100s; default timeout 300 -> keep
        MagicMock(id=2, pinned=False, idle_timeout_s=None,
                  last_request_at=now - 100, status="ready"),
        # pinned -> keep regardless
        MagicMock(id=3, pinned=True, idle_timeout_s=None,
                  last_request_at=now - 10_000, status="ready"),
    ]
    manager = MagicMock()
    manager.stop = AsyncMock()

    list_ready = MagicMock(return_value=deployments)

    reaper = Reaper(
        manager=manager,
        list_ready=list_ready,
        default_idle_timeout_s=300,
        now_fn=lambda: now,
    )
    await reaper.tick_once()

    manager.stop.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_reaper_respects_per_deployment_timeout():
    now = 1_000_000
    deployments = [
        # idle 100s; per-deployment 60 -> evict
        MagicMock(id=1, pinned=False, idle_timeout_s=60,
                  last_request_at=now - 100, status="ready"),
        # idle 100s; per-deployment 600 -> keep
        MagicMock(id=2, pinned=False, idle_timeout_s=600,
                  last_request_at=now - 100, status="ready"),
    ]
    manager = MagicMock()
    manager.stop = AsyncMock()
    reaper = Reaper(
        manager=manager,
        list_ready=MagicMock(return_value=deployments),
        default_idle_timeout_s=300,
        now_fn=lambda: now,
    )
    await reaper.tick_once()
    manager.stop.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_reaper_skips_when_last_request_at_none():
    now = 1_000_000
    deployments = [
        MagicMock(id=1, pinned=False, idle_timeout_s=None,
                  last_request_at=None, status="ready"),
    ]
    manager = MagicMock()
    manager.stop = AsyncMock()
    reaper = Reaper(
        manager=manager,
        list_ready=MagicMock(return_value=deployments),
        default_idle_timeout_s=300,
        now_fn=lambda: now,
    )
    await reaper.tick_once()
    manager.stop.assert_not_called()


@pytest.mark.asyncio
async def test_reaper_evicts_after_real_time_idle(monkeypatch):
    """End-to-end timing test: reaper, started normally, evicts within tick_s."""
    now_seq = [1_000_000]

    def fake_now():
        return now_seq[0]

    # Two deployments: id=1 has idle_timeout_s=1, id=2 has idle_timeout_s=10
    deployments = [
        MagicMock(id=1, pinned=False, idle_timeout_s=1,
                  last_request_at=now_seq[0] - 0.5, status="ready"),
        MagicMock(id=2, pinned=False, idle_timeout_s=10,
                  last_request_at=now_seq[0] - 0.5, status="ready"),
    ]

    manager = MagicMock()
    manager.stop = AsyncMock()

    reaper = Reaper(
        manager=manager,
        list_ready=MagicMock(return_value=deployments),
        default_idle_timeout_s=300,
        tick_s=0.2,           # tick every 200 ms
        now_fn=fake_now,
    )
    reaper.start()
    # Advance virtual time past id=1's 1 s timeout
    await asyncio.sleep(0.3)
    now_seq[0] += 2
    await asyncio.sleep(0.3)
    await reaper.stop()

    # id=1 should be evicted, id=2 not
    manager.stop.assert_any_call(1)
    for call in manager.stop.call_args_list:
        assert call.args[0] != 2
