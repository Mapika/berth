import asyncio
from dataclasses import dataclass

from berth.lifecycle.reaper import Reaper


@dataclass
class _Dep:
    id: int
    pinned: bool = False
    status: str = "ready"
    last_request_at: float = 0.0   # epoch 0 => very idle
    idle_timeout_s: int | None = 1
    source: str = "managed"


class _Manager:
    def __init__(self):
        self.stopped = []
    async def stop(self, dep_id):
        self.stopped.append(dep_id)


def test_reaper_evicts_managed_but_not_adopted():
    mgr = _Manager()
    deps = [_Dep(id=1, source="managed"), _Dep(id=2, source="adopted")]
    reaper = Reaper(manager=mgr, list_ready=lambda: deps,
                    now_fn=lambda: 10_000.0)
    asyncio.run(reaper.tick_once())
    assert mgr.stopped == [1]
