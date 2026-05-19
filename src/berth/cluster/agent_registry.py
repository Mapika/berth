from __future__ import annotations

from berth.cluster.agent_link import AgentLink


class AgentRegistry:
    """In-memory map of node_id -> AgentLink. Populated by:
      - daemon startup (for the in-process LocalAgentLink), and
      - LeaderHub when a remote agent's WS handshake completes.
    """

    def __init__(self) -> None:
        self._by_node: dict[int, AgentLink] = {}

    def register(self, link: AgentLink) -> None:
        self._by_node[link.node_id] = link

    def unregister(self, node_id: int) -> None:
        self._by_node.pop(node_id, None)

    def get(self, node_id: int) -> AgentLink | None:
        return self._by_node.get(node_id)

    def all(self) -> list[AgentLink]:
        return list(self._by_node.values())
