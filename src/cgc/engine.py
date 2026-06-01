"""CGC Engine -- the main orchestrator that wires all layers together.

The :class:`CGCEngine` is the top-level entry point for the Computational
Governance system.  It instantiates every subsystem (foundation, relay,
authority, governance) and exposes high-level async operations for task
submission, completion, delegation, feedback, elevation, appeals, and
periodic governance cycles.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from cgc.authority.capability_network import CapabilityNetwork
from cgc.foundation.artifact import ArtifactStore
from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.foundation.task_state import TaskStateCore
from cgc.governance.constitution import ConstitutionEngine
from cgc.governance.discipline import DisciplineSystem
from cgc.governance.elevation import ElevationEngine
from cgc.governance.feedback import FeedbackAttributionLayer
from cgc.governance.reputation import ReputationRating
from cgc.models import (
    AgentLevel,
    AgentRole,
    AgentStatus,
    ConstitutionRuling,
    ElevationDecision,
    ElevationRequest,
    FeedbackAttribution,
    FeedbackCategory,
    TaskState,
)
from cgc.relay.dispatcher import Dispatcher
from cgc.relay.message_bus import MessageBus

if TYPE_CHECKING:
    from cgc.models.messages import Message


class CGCEngine:
    """Main orchestrator for the Computational Governance system.

    Wires together the four architectural layers:

    * **Foundation** -- task state, agent registry, artifact store,
      observability log.
    * **Relay** -- message bus and dispatcher.
    * **Authority** -- capability network and projection engine.
    * **Governance** -- reputation, elevation, discipline, constitution,
      and feedback attribution.
    """

    def __init__(self) -> None:
        # ------------------------------------------------------------------
        # Foundation layer
        # ------------------------------------------------------------------
        self.task_core = TaskStateCore()
        self.registry = AgentRegistry()
        self.artifact_store = ArtifactStore()
        self.observability = ObservabilityLog()

        # ------------------------------------------------------------------
        # Relay layer
        # ------------------------------------------------------------------
        self.message_bus = MessageBus()
        self.dispatcher = Dispatcher(
            self.message_bus, self.task_core, self.registry, None, self.observability
        )

        # ------------------------------------------------------------------
        # Authority layer
        # ------------------------------------------------------------------
        self.capability_network = CapabilityNetwork(
            self.registry, self.task_core, self.observability
        )
        self.dispatcher._capability_network = self.capability_network

        # ------------------------------------------------------------------
        # Governance layer
        # ------------------------------------------------------------------
        self.reputation = ReputationRating(self.task_core, self.observability)
        self.elevation = ElevationEngine(
            self.registry, self.reputation, self.observability
        )
        self.discipline = DisciplineSystem(self.registry, self.observability)
        self.constitution = ConstitutionEngine(self.registry, self.observability)
        self.feedback = FeedbackAttributionLayer(
            self.task_core, self.registry, self.observability
        )

        self._scheduled_tasks: list[asyncio.Task] = []

    # ==================================================================
    # Lifecycle
    # ==================================================================

    async def start(self) -> None:
        """Start background tasks (governance tick, etc.)."""
        task = asyncio.create_task(self._governance_loop())
        self._scheduled_tasks.append(task)

    async def stop(self) -> None:
        """Cancel background tasks."""
        for task in self._scheduled_tasks:
            task.cancel()
        # Wait for all tasks to finish cancellation.
        if self._scheduled_tasks:
            await asyncio.gather(*self._scheduled_tasks, return_exceptions=True)
        self._scheduled_tasks.clear()

    async def _governance_loop(self) -> None:
        """Periodic governance cycle runner."""
        try:
            while True:
                await asyncio.sleep(60)  # run every 60 seconds
                await self.run_governance_cycle()
        except asyncio.CancelledError:
            return

    # ==================================================================
    # Task submission & completion
    # ==================================================================

    async def submit_task(
        self,
        description: str,
        constraints: dict | None = None,
    ) -> tuple[str, TaskState]:
        """Submit a new task and auto-assign it to the best available planner.

        Returns
        -------
        tuple[str, TaskState]
            The generated task ID and the initial task state.
        """
        task_id = uuid.uuid4().hex
        task = await self.dispatcher.submit_task(task_id, description, constraints)

        # Auto-assign to best available planner
        agent = await self.dispatcher.select_agent(role=AgentRole.PLANNER)
        if agent:
            await self.dispatcher.assign_task(task_id, agent.agent_id)
            await self.capability_network.project_for_agent(
                agent.agent_id, task_id
            )

        return task_id, task

    async def complete_task_step(
        self,
        agent_id: str,
        task_id: str,
        result: dict,
        artifact_type: str | None = None,
    ) -> Message:
        """Record a task step result from an agent.

        If *artifact_type* is provided the result is also stored as an
        artifact.  Reputation is updated with a successful completion.
        """
        msg = await self.dispatcher.receive_result(agent_id, task_id, result)

        # Store artifact if provided
        if artifact_type and result:
            await self.artifact_store.store(
                None,
                artifact_type,
                result,
                producer=agent_id,
                task_id=task_id,
            )

        # Update reputation
        await self.reputation.record_task_completion(
            agent_id, task_id, success=True, first_pass=True
        )

        await self.observability.log(
            "task_step_complete",
            "engine",
            {"agent_id": agent_id, "task_id": task_id},
        )

        return msg

    async def report_failure(
        self,
        agent_id: str,
        task_id: str,
        error: str,
    ) -> Message:
        """Record a task failure from an agent.

        Reputation is updated with a failed completion, and discipline
        metric gates are checked.
        """
        result = {"status": "failed", "error": error}
        msg = await self.dispatcher.receive_result(agent_id, task_id, result)

        await self.reputation.record_task_completion(
            agent_id, task_id, success=False, first_pass=False
        )

        # Check discipline triggers
        suggestions = await self.discipline.check_metric_gates(agent_id)
        for s in suggestions:
            await self.observability.log(
                "discipline_suggestion",
                "engine",
                {"agent_id": agent_id, "action": s.action},
            )

        return msg

    # ==================================================================
    # Delegation
    # ==================================================================

    async def delegate(
        self,
        parent_task_id: str,
        child_description: str,
        target_role: AgentRole = AgentRole.EXECUTOR,
        capability_budget: list[str] | None = None,
    ) -> str | None:
        """Delegate a subtask from *parent_task_id*.

        Selects the best available agent for *target_role* (excluding the
        parent task owner) and delegates the subtask with a capability
        budget annotation (Section 16.2).

        Parameters
        ----------
        parent_task_id:
            The parent task to create a subtask under.
        child_description:
            Description of the subtask.
        target_role:
            The required agent role for the subtask.
        capability_budget:
            Optional list of capabilities the subtask is budgeted to use.
            This is passed to the capability network to scope the agent's
            projected capabilities for this subtask.

        Returns
        -------
        str | None
            The child task ID, or ``None`` if the parent task does not
            exist or no suitable agent is available.
        """
        child_id = uuid.uuid4().hex

        parent = await self.task_core.get_task(parent_task_id)
        if not parent:
            return None

        await self.task_core.create_task(child_id, parent_task_id)
        await self.task_core.add_child_task(parent_task_id, child_id)

        exclude = [parent.owner] if parent.owner else []
        agent = await self.dispatcher.select_agent(
            role=target_role, exclude=exclude
        )
        if agent:
            delegation_spec: dict = {
                "description": child_description,
                "role": target_role.value,
            }
            # Section 16.2: high-level agents annotate capability budget
            if capability_budget is not None:
                delegation_spec["capability_budget"] = capability_budget
                await self.task_core.set_constraints(
                    child_id,
                    {"capability_budget": capability_budget},
                )

            await self.dispatcher.delegate_subtask(
                parent_task_id,
                child_id,
                agent.agent_id,
                delegation_spec,
            )
            await self.capability_network.project_for_agent(
                agent.agent_id, child_id
            )
            return child_id

        return None

    # ==================================================================
    # Human feedback
    # ==================================================================

    async def submit_human_feedback(
        self,
        task_id: str,
        category: FeedbackCategory,
        description: str,
        agent_weights: list[dict] | None = None,
    ) -> list[FeedbackAttribution]:
        """Submit human feedback for a task and attribute it to agents.

        Returns
        -------
        list[FeedbackAttribution]
            The feedback attribution records created.
        """
        attributions = await self.feedback.attribute_feedback(
            task_id, category, description, agent_weights
        )

        for attr in attributions:
            await self.observability.log(
                "human_feedback",
                "engine",
                {
                    "agent_id": attr.agent_id,
                    "category": category.value,
                    "weight": attr.responsibility_weight,
                },
            )
            await self.reputation.update_profile(attr.agent_id)

        return attributions

    # ==================================================================
    # Elevation
    # ==================================================================

    async def request_elevation(
        self,
        agent_id: str,
        target_level: AgentLevel,
    ) -> ElevationDecision:
        """Request an elevation (promotion) for an agent.

        If the agent is eligible and the evaluation approves, the agent's
        level is updated in the registry and capability tables are
        refreshed.
        """
        try:
            request = await self.elevation.request_elevation(
                agent_id, target_level
            )
        except ValueError:
            # Not eligible -- return a denied decision
            fallback_request = ElevationRequest.create(
                agent_id=agent_id,
                target_level=target_level,
            )
            return ElevationDecision.create(
                request=fallback_request,
                approved=False,
                reason="Agent is not eligible for elevation.",
                committee_scores={"eligible": False},
            )

        decision = await self.elevation.evaluate(request)
        if decision.approved:
            await self.registry.update_level(agent_id, target_level)
            await self.capability_network.on_agent_level_change(
                agent_id, target_level
            )

        await self.observability.log(
            "elevation_decision",
            "engine",
            {"agent_id": agent_id, "approved": decision.approved},
        )
        return decision

    # ==================================================================
    # Constitution / Appeals
    # ==================================================================

    async def appeal(
        self,
        appellant_id: str,
        contested_id: str,
        contested_type: str,
        grounds: str,
        evidence: list[str],
    ) -> ConstitutionRuling:
        """File an appeal and immediately review it.

        Returns
        -------
        ConstitutionRuling
            The ruling produced by the constitutional review.
        """
        appeal = await self.constitution.file_appeal(
            appellant_id, contested_id, contested_type, grounds, evidence
        )
        ruling = await self.constitution.review_appeal(appeal.appeal_id)

        await self.observability.log(
            "constitution_ruling",
            "engine",
            {
                "appeal_id": appeal.appeal_id,
                "verdict": ruling.verdict.value,
            },
        )
        return ruling

    # ==================================================================
    # Governance cycle
    # ==================================================================

    async def run_governance_cycle(self) -> None:
        """Periodic governance check (Section 19).

        Scheduled tasks:
        * Promotion windows
        * High-level ratio checks
        * Gate threshold updates (dynamic tightening/relaxing)
        * Long-term performance collection
        * Discipline candidate scanning
        """
        # Check high level ratio and tighten if needed
        ratio = await self.elevation.check_high_level_ratio()
        if ratio > 0.3:
            await self.elevation.tighten_promotion()
        elif ratio < 0.15 and not self.elevation.config.get("promotion_window_open", True):
            # Relax: if ratio drops well below threshold, reopen window
            await self.elevation.open_promotion_window()

        # Process pending elevation requests
        await self.elevation.process_pending()

        # Scan for discipline candidates
        agents = await self.registry.find_available()
        for agent in agents:
            await self.discipline.check_metric_gates(agent.agent_id)

        # Refresh all reputation profiles (long-term performance collection)
        for agent in agents:
            await self.reputation.update_profile(agent.agent_id)

        # Refresh capability tables for all active agents
        await self.capability_network.refresh_all()

        # Dispatcher tick for auto-advancing stuck tasks
        await self.dispatcher.tick()

    # ==================================================================
    # System status
    # ==================================================================

    async def get_system_status(self) -> dict:
        """Get overall system status.

        Returns
        -------
        dict
            Summary of agents, tasks, and governance state.
        """
        agents = await self.registry.list_all()
        tasks = await self.task_core.get_active_tasks()
        return {
            "total_agents": len(agents),
            "active_agents": len(
                [a for a in agents if a.status == AgentStatus.ACTIVE]
            ),
            "frozen_agents": len(
                [a for a in agents if a.status == AgentStatus.FROZEN]
            ),
            "active_tasks": len(tasks),
            "high_level_ratio": await self.elevation.check_high_level_ratio(),
            "promotion_window_open": self.elevation.config.get(
                "promotion_window_open", True
            ),
        }
