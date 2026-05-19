from __future__ import annotations

import threading
from collections import OrderedDict


class RoutingAffinity:
    """Bounded LRU `affinity_key → node_id` map.

    Best-effort. Lost on process restart. Cleared per-node on node loss.
    Thread-safe with a single lock; held for microseconds per op.
    """

    def __init__(self, *, capacity: int = 10_000) -> None:
        self._capacity = capacity
        self._data: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def lookup(self, key: str) -> int | None:
        with self._lock:
            v = self._data.get(key)
            if v is not None:
                self._data.move_to_end(key)
            return v

    def set(self, key: str, *, node_id: int) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = node_id
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def evict_node(self, node_id: int) -> None:
        with self._lock:
            to_drop = [k for k, v in self._data.items() if v == node_id]
            for k in to_drop:
                self._data.pop(k, None)
