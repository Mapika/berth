from __future__ import annotations

from berth.cluster.agent_link import AgentLink


class AgentRegistry:
    """In-memory map of node_id -> AgentLink. Populated by:
      - daemon startup (for the in-process LocalAgentLink), and
      - LeaderHub when a remote agent's WS handshake completes.
    """

    def __init__(self) -> None:
        self._by_node: dict[int, AgentLink] = {}

    def register(self, link: AgentLink) -> AgentLink | None:
        """Install `link` for its node_id, replacing any previous registration.

        Returns the displaced link (if any) so the caller can shut it down
        and close its underlying transport. This is the eviction-on-collision
        path: when a malicious or buggy agent opens a second WebSocket while
        an earlier one is still attached, we want the newer connection to win
        but we must NOT leave the old session dangling on the wire (it would
        keep consuming heartbeats and racing the new link on DB state).
        """
        previous = self._by_node.get(link.node_id)
        self._by_node[link.node_id] = link
        return previous if previous is not link else None

    def unregister(self, link: AgentLink) -> bool:
        """Pop the registration only if `link` is the currently-active one.

        Returns True if we actually removed something. The identity check
        prevents a stale link's finally-block from popping a newer link that
        has displaced it (which would mark the node as unreachable even
        though it's still online via the newer connection).
        """
        node_id = link.node_id
        current = self._by_node.get(node_id)
        if current is link:
            del self._by_node[node_id]
            return True
        return False

    def get(self, node_id: int) -> AgentLink | None:
        return self._by_node.get(node_id)

    def all(self) -> list[AgentLink]:
        return list(self._by_node.values())
