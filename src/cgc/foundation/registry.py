from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from cgc.models import AgentLevel, AgentProfile, AgentRole, AgentStatus

if TYPE_CHECKING:
    pass


class AgentRegistry:
    """In-memory registry for tracking agent profiles."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentProfile] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def register(self, agent: AgentProfile) -> AgentProfile:
        """Register a new agent. Raises ValueError if the agent_id already exists."""
        async with self._lock:
            if agent.agent_id in self._agents:
                raise ValueError(
                    f"Agent with id '{agent.agent_id}' is already registered."
                )
            self._agents[agent.agent_id] = agent
            return agent

    async def get(self, agent_id: str) -> AgentProfile | None:
        """Return the agent profile for *agent_id*, or None if not found."""
        async with self._lock:
            return self._agents.get(agent_id)

    async def update_status(self, agent_id: str, status: AgentStatus) -> AgentProfile:
        """Set the status of an agent and return the updated profile."""
        async with self._lock:
            agent = self._agents[agent_id]
            updated = replace(agent, status=status)
            self._agents[agent_id] = updated
            return updated

    async def update_level(self, agent_id: str, level: AgentLevel) -> AgentProfile:
        """Set the level of an agent and return the updated profile."""
        async with self._lock:
            agent = self._agents[agent_id]
            updated = replace(agent, level=level)
            self._agents[agent_id] = updated
            return updated

    async def update_load(self, agent_id: str, delta: int) -> AgentProfile:
        """Increment (or decrement) the load of an agent by *delta*."""
        async with self._lock:
            agent = self._agents[agent_id]
            updated = replace(agent, load=agent.load + delta)
            self._agents[agent_id] = updated
            return updated

    async def add_task(self, agent_id: str, task_id: str) -> AgentProfile:
        """Add *task_id* to the agent's current task list."""
        async with self._lock:
            agent = self._agents[agent_id]
            updated = replace(
                agent, current_task_ids=[*agent.current_task_ids, task_id]
            )
            self._agents[agent_id] = updated
            return updated

    async def remove_task(self, agent_id: str, task_id: str) -> AgentProfile:
        """Remove *task_id* from the agent's current task list."""
        async with self._lock:
            agent = self._agents[agent_id]
            updated = replace(
                agent,
                current_task_ids=[
                    t for t in agent.current_task_ids if t != task_id
                ],
            )
            self._agents[agent_id] = updated
            return updated

    async def update_capabilities(
        self, agent_id: str, capabilities: list[str]
    ) -> AgentProfile:
        """Replace the agent's capability list."""
        async with self._lock:
            agent = self._agents[agent_id]
            updated = replace(agent, capabilities=capabilities)
            self._agents[agent_id] = updated
            return updated

    async def find_available(
        self,
        role: AgentRole | None = None,
        min_level: AgentLevel | None = None,
        max_load: int | None = None,
    ) -> list[AgentProfile]:
        """Return agents with ACTIVE status matching the optional filters."""
        async with self._lock:
            results: list[AgentProfile] = []
            for agent in self._agents.values():
                if agent.status != AgentStatus.ACTIVE:
                    continue
                if role is not None and agent.role != role:
                    continue
                if min_level is not None and agent.level < min_level:
                    continue
                if max_load is not None and agent.load > max_load:
                    continue
                results.append(agent)
            return results

    async def retire(self, agent_id: str) -> AgentProfile:
        """Set the agent's status to RETIRED."""
        return await self.update_status(agent_id, AgentStatus.RETIRED)

    async def freeze(self, agent_id: str) -> AgentProfile:
        """Set the agent's status to FROZEN."""
        return await self.update_status(agent_id, AgentStatus.FROZEN)

    async def unfreeze(self, agent_id: str) -> AgentProfile:
        """Set the agent's status back to ACTIVE."""
        return await self.update_status(agent_id, AgentStatus.ACTIVE)

    async def list_all(self) -> list[AgentProfile]:
        """Return all registered agent profiles."""
        async with self._lock:
            return list(self._agents.values())

    async def count_by_level(self) -> dict[AgentLevel, int]:
        """Return a count of agents grouped by level."""
        async with self._lock:
            counts: dict[AgentLevel, int] = {}
            for agent in self._agents.values():
                counts[agent.level] = counts.get(agent.level, 0) + 1
            return counts
