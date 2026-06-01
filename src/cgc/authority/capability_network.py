"""Capability Network for the CGC Authority Protocol.

Maintains the full capability topology and projects capability tables
to individual agents via the :class:`ProjectionEngine`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.foundation.task_state import TaskStateCore
from cgc.models import (
    AgentLevel,
    AgentProfile,
    AgentRole,
    ContractType,
    CapabilityHandle,
    CapabilityRule,
    CapabilityStrength,
    CapabilityTable,
    TaskStage,
    TaskState,
)

from cgc.authority.projection import ProjectionEngine


class CapabilityNetwork:
    """Maintains the full capability topology and projects to agents.

    Parameters
    ----------
    registry:
        Agent registry used to look up agent profiles.
    task_core:
        Task state core used to look up task context.
    observability:
        Observability log for recording projection events.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        task_core: TaskStateCore,
        observability: ObservabilityLog,
    ) -> None:
        self._registry = registry
        self._task_core = task_core
        self._observability = observability

        self.projection_engine: ProjectionEngine = ProjectionEngine()
        self.capability_tables: dict[str, CapabilityTable] = {}
        self.full_capability_set: list[str] = []

        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Capability registration
    # ------------------------------------------------------------------

    async def register_capabilities(self, capabilities: list[str]) -> None:
        """Register capabilities into the full set.

        Duplicates are silently ignored.
        """
        async with self._lock:
            existing = set(self.full_capability_set)
            for cap in capabilities:
                if cap not in existing:
                    self.full_capability_set.append(cap)
                    existing.add(cap)

    async def get_full_capability_set(self) -> list[str]:
        """Return a copy of the full capability set."""
        async with self._lock:
            return list(self.full_capability_set)

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------

    async def project_for_agent(
        self,
        agent_id: str,
        task_id: str | None = None,
    ) -> CapabilityTable:
        """Compute or refresh the capability table for an agent.

        Steps:
        1. Get agent profile from registry.
        2. Get task state if task_id is provided.
        3. Determine available capabilities (external executors get a
           restricted surface -- Section 17).
        4. Call projection engine to compute handles.
        5. Build a CapabilityTable with the handles.
        6. Cache it.
        7. Log to observability.
        """
        # 1 -- agent profile
        agent = await self._registry.get(agent_id)
        if agent is None:
            raise ValueError(f"Agent not found: {agent_id}")

        # 2 -- optional task state
        task_state: TaskState | None = None
        if task_id is not None:
            task_state = await self._task_core.get_task(task_id)

        # 3 -- determine available capabilities
        async with self._lock:
            available = list(self.full_capability_set)

        # External executors (Section 17): the capability network does NOT
        # directly project internal capabilities to external agents.
        # They only get "external_interface" capabilities.
        if agent.contract_type == ContractType.EXTERNAL:
            available = [c for c in available if c.startswith("external_interface.")]

        # 4 -- compute handles
        handles: list[CapabilityHandle] = self.projection_engine.project(
            agent=agent,
            task_state=task_state,
            available_capabilities=available,
        )

        # 4 -- build table
        context_hash = ProjectionEngine.compute_context_hash(
            agent_id=agent_id,
            task_id=task_id,
            level=agent.level.value,
            role=agent.role.value,
        )
        table = CapabilityTable(
            agent_id=agent_id,
            handles=handles,
            valid_until=None,
            context_hash=context_hash,
        )

        # 5 -- cache
        async with self._lock:
            self.capability_tables[agent_id] = table

        # 6 -- observability
        await self._observability.log(
            event_type="capability_table_computed",
            source="CapabilityNetwork",
            details={
                "agent_id": agent_id,
                "task_id": task_id,
                "handle_count": len(handles),
                "context_hash": context_hash,
            },
            agent_id=agent_id,
            task_id=task_id,
        )

        return table

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    async def refresh_all(self) -> dict[str, CapabilityTable]:
        """Recompute capability tables for all active agents."""
        agents = await self._registry.find_available()

        results: dict[str, CapabilityTable] = {}
        for agent in agents:
            task_id: str | None = None
            if agent.current_task_ids:
                task_id = agent.current_task_ids[0]
            table = await self.project_for_agent(agent.agent_id, task_id)
            results[agent.agent_id] = table

        await self._observability.log(
            event_type="capability_refresh_all",
            source="CapabilityNetwork",
            details={"agent_count": len(results)},
        )

        return results

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    async def invalidate(self, agent_id: str) -> None:
        """Clear the cached capability table for an agent."""
        async with self._lock:
            self.capability_tables.pop(agent_id, None)

    async def get_table(self, agent_id: str) -> CapabilityTable | None:
        """Return the cached capability table for an agent, or None."""
        async with self._lock:
            return self.capability_tables.get(agent_id)

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    async def handle_capability_request(
        self,
        agent_id: str,
        requested: list[str],
        context: dict[str, Any],
    ) -> dict[str, bool]:
        """Check if an agent can access requested capabilities.

        Returns a mapping from capability name to access boolean.
        If the agent has no cached table, one is computed first.
        """
        table = await self.get_table(agent_id)
        if table is None:
            task_id = context.get("task_id")
            table = await self.project_for_agent(agent_id, task_id)

        permitted_names = {handle.name for handle in table.handles}
        result: dict[str, bool] = {}
        for cap in requested:
            result[cap] = cap in permitted_names

        await self._observability.log(
            event_type="capability_request",
            source="CapabilityNetwork",
            details={
                "agent_id": agent_id,
                "requested": requested,
                "result": result,
            },
            agent_id=agent_id,
            task_id=context.get("task_id"),
        )

        return result

    # ------------------------------------------------------------------
    # Event-driven hooks
    # ------------------------------------------------------------------

    async def on_agent_level_change(
        self,
        agent_id: str,
        new_level: AgentLevel,
    ) -> None:
        """Invalidate and recompute when an agent's level changes."""
        await self.invalidate(agent_id)
        await self.project_for_agent(agent_id)

        await self._observability.log(
            event_type="capability_level_change",
            source="CapabilityNetwork",
            details={
                "agent_id": agent_id,
                "new_level": new_level.name,
            },
            agent_id=agent_id,
        )

    async def on_task_stage_change(
        self,
        task_id: str,
        new_stage: TaskStage,
    ) -> None:
        """Refresh capability tables for agents working on the given task."""
        agents = await self._registry.list_all()
        affected: list[str] = []

        for agent in agents:
            if task_id in agent.current_task_ids:
                await self.invalidate(agent.agent_id)
                await self.project_for_agent(agent.agent_id, task_id)
                affected.append(agent.agent_id)

        await self._observability.log(
            event_type="capability_task_stage_change",
            source="CapabilityNetwork",
            details={
                "task_id": task_id,
                "new_stage": new_stage.name,
                "affected_agents": affected,
            },
            task_id=task_id,
        )
