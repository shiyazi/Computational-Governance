"""Control hub that sits inside the CGC message network.

The :class:`Dispatcher` is the orchestrator that wires together the
message bus, task state store, agent registry, capability network, and
observability log.  It exposes high-level operations (submit, assign,
delegate, etc.) that mutate task state *and* emit the appropriate
messages onto the bus so that every other component stays informed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cgc.models.agent import AgentLevel, AgentProfile, AgentRole, AgentStatus
from cgc.models.messages import (
    CapabilityRequest,
    GovernanceMessage,
    Message,
    MessageType,
)
from cgc.models.task import TaskStage, TaskState

if TYPE_CHECKING:
    from cgc.foundation.observability import ObservabilityLog
    from cgc.foundation.registry import AgentRegistry
    from cgc.foundation.task_state import TaskStateCore
    from cgc.relay.message_bus import MessageBus


class Dispatcher:
    """Central dispatch controller for the CGC relay layer.

    Parameters
    ----------
    message_bus:
        The :class:`~cgc.relay.message_bus.MessageBus` instance used for
        all inter-component communication.
    task_core:
        The single source of truth for task state.
    registry:
        The agent registry used for agent look-ups and selection.
    capability_network:
        Reference to the capability / authority network (currently kept
        as an opaque reference for forward-compatibility).
    observability:
        The system-wide observability log.
    """

    def __init__(
        self,
        message_bus: MessageBus,
        task_core: TaskStateCore,
        registry: AgentRegistry,
        capability_network: Any,
        observability: ObservabilityLog,
    ) -> None:
        self._bus = message_bus
        self._task_core = task_core
        self._registry = registry
        self._capability_network = capability_network
        self._obs = observability

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    async def submit_task(
        self,
        task_id: str,
        description: str,
        constraints: dict[str, Any] | None = None,
    ) -> TaskState:
        """Submit a new task from an external user.

        Creates the task in :attr:`TaskStage.PENDING`, records it in
        *task_core*, publishes a ``TASK_SUBMIT`` message, and logs the
        event to observability.
        """
        metadata: dict[str, Any] = {"description": description}
        if constraints:
            metadata["constraints"] = constraints

        task = await self._task_core.create_task(
            task_id=task_id,
            metadata=metadata,
        )

        if constraints:
            await self._task_core.set_constraints(task_id, constraints)

        msg = Message.create(
            msg_type=MessageType.TASK_SUBMIT,
            sender="dispatcher",
            payload={"description": description, "constraints": constraints or {}},
            task_id=task_id,
        )
        await self._bus.publish(msg)

        await self._obs.log(
            event_type="dispatch_decision",
            source="dispatcher",
            details={"action": "submit_task", "task_id": task_id},
            task_id=task_id,
        )

        return task

    async def assign_task(self, task_id: str, agent_id: str) -> Message:
        """Assign *task_id* to *agent_id*.

        Updates the task owner and participant list, increments the
        agent's load, publishes a ``TASK_ASSIGN`` message, and logs the
        decision.
        """
        await self._task_core.set_owner(task_id, agent_id)
        await self._task_core.add_participant(task_id, agent_id)
        await self._task_core.update_stage(task_id, TaskStage.DISPATCHED)
        await self._registry.update_load(agent_id, delta=1)
        await self._registry.add_task(agent_id, task_id)

        msg = Message.create(
            msg_type=MessageType.TASK_ASSIGN,
            sender="dispatcher",
            receiver=agent_id,
            payload={"agent_id": agent_id},
            task_id=task_id,
        )
        await self._bus.publish(msg)

        await self._obs.log(
            event_type="dispatch_decision",
            source="dispatcher",
            details={"action": "assign_task", "task_id": task_id, "agent_id": agent_id},
            task_id=task_id,
            agent_id=agent_id,
        )

        return msg

    async def receive_result(
        self,
        agent_id: str,
        task_id: str,
        result: dict[str, Any],
    ) -> Message:
        """An agent reports a task result.

        Publishes a ``TASK_RESULT`` message, advances the task turn,
        decrements the agent's load, and removes the task from the
        agent's active list.
        """
        msg = Message.create(
            msg_type=MessageType.TASK_RESULT,
            sender=agent_id,
            receiver="dispatcher",
            payload=result,
            task_id=task_id,
        )
        await self._bus.publish(msg)

        task = await self._task_core.advance_turn(task_id)
        # Advance round when turn resets or on every N results (round boundary)
        # A round advances when multiple participants have each taken a turn.
        if task.participants and task.turn % len(task.participants) == 0 and task.turn > 0:
            await self._task_core.advance_round(task_id)

        await self._registry.update_load(agent_id, delta=-1)
        await self._registry.remove_task(agent_id, task_id)

        await self._obs.log(
            event_type="agent_task_acceptance",
            source="dispatcher",
            details={"action": "receive_result", "task_id": task_id, "agent_id": agent_id},
            task_id=task_id,
            agent_id=agent_id,
        )

        return msg

    # ------------------------------------------------------------------
    # Delegation
    # ------------------------------------------------------------------

    async def delegate_subtask(
        self,
        parent_task_id: str,
        child_task_id: str,
        target_agent_id: str,
        delegation_spec: dict[str, Any],
    ) -> Message:
        """A higher-level agent delegates a subtask.

        Creates the child task, links it to the parent, assigns it to
        *target_agent_id*, and publishes a ``TASK_DELEGATE`` message.

        Cycle detection: walks the parent chain to ensure the child task
        is not already an ancestor (prevents circular delegation).
        """
        # -- Cycle detection: walk parent chain --
        visited: set[str] = {child_task_id}
        ancestor_id: str | None = parent_task_id
        while ancestor_id is not None:
            if ancestor_id in visited:
                raise ValueError(
                    f"Circular delegation detected: task {child_task_id} "
                    f"is already an ancestor of {parent_task_id}"
                )
            visited.add(ancestor_id)
            ancestor_task = await self._task_core.get_task(ancestor_id)
            ancestor_id = ancestor_task.parent_task_id if ancestor_task else None

        # Create the child task linked to the parent.
        child_task = await self._task_core.create_task(
            task_id=child_task_id,
            parent_task_id=parent_task_id,
            metadata={"delegation_spec": delegation_spec},
        )
        await self._task_core.add_child_task(parent_task_id, child_task_id)

        # Assign to the target agent.
        await self._task_core.set_owner(child_task_id, target_agent_id)
        await self._task_core.add_participant(child_task_id, target_agent_id)
        await self._task_core.update_stage(child_task_id, TaskStage.DISPATCHED)
        await self._registry.update_load(target_agent_id, delta=1)
        await self._registry.add_task(target_agent_id, child_task_id)

        msg = Message.create(
            msg_type=MessageType.TASK_DELEGATE,
            sender="dispatcher",
            receiver=target_agent_id,
            payload={
                "parent_task_id": parent_task_id,
                "child_task_id": child_task_id,
                "delegation_spec": delegation_spec,
            },
            task_id=child_task_id,
        )
        await self._bus.publish(msg)

        await self._obs.log(
            event_type="dispatch_decision",
            source="dispatcher",
            details={
                "action": "delegate_subtask",
                "parent_task_id": parent_task_id,
                "child_task_id": child_task_id,
                "target_agent_id": target_agent_id,
            },
            task_id=child_task_id,
            agent_id=target_agent_id,
        )

        return msg

    # ------------------------------------------------------------------
    # Governance routing
    # ------------------------------------------------------------------

    async def route_to_governance(
        self,
        governance_type: str,
        payload: dict[str, Any],
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> Message:
        """Route a governance event onto the message bus.

        The message is typed as ``GOVERNANCE_EVENT`` and includes the
        *governance_type* in its payload so governance components can
        filter appropriately.  The message is also explicitly forwarded
        to the relevant governance targets per Section 5.2(2).
        """
        gov_payload = {"governance_type": governance_type, **payload}

        msg = GovernanceMessage.create(
            msg_type=MessageType.GOVERNANCE_EVENT,
            sender="dispatcher",
            governance_type=governance_type,
            payload=gov_payload,
            task_id=task_id,
        )
        await self._bus.publish(msg)

        # Explicit multi-target forwarding (Section 5.2(2))
        targets = [
            "elevation_engine",
            "discipline_system",
            "constitution_engine",
            "reputation_rating",
            "feedback_attribution",
            "log_and_state",
        ]
        for target in targets:
            targeted_msg = Message.create(
                msg_type=MessageType.GOVERNANCE_EVENT,
                sender="dispatcher",
                receiver=target,
                payload={**gov_payload, "target": target},
                task_id=task_id,
            )
            await self._bus.publish(targeted_msg)

        await self._obs.log(
            event_type="dispatch_decision",
            source="dispatcher",
            details={
                "action": "route_to_governance",
                "governance_type": governance_type,
                "forwarded_to": targets,
            },
            task_id=task_id,
            agent_id=agent_id,
        )

        return msg

    # ------------------------------------------------------------------
    # Capability routing
    # ------------------------------------------------------------------

    async def route_capability_request(
        self,
        agent_id: str,
        requested_capabilities: list[str],
        context: dict[str, Any],
    ) -> Message:
        """Route a capability request to the authority network."""
        msg = CapabilityRequest.create(
            sender="dispatcher",
            agent_id=agent_id,
            requested_capabilities=requested_capabilities,
            context=context,
        )
        await self._bus.publish(msg)

        await self._obs.log(
            event_type="capability_request",
            source="dispatcher",
            details={
                "action": "route_capability_request",
                "agent_id": agent_id,
                "requested_capabilities": requested_capabilities,
            },
            agent_id=agent_id,
        )

        return msg

    # ------------------------------------------------------------------
    # Agent selection
    # ------------------------------------------------------------------

    async def select_agent(
        self,
        role: AgentRole | None = None,
        min_level: AgentLevel | None = None,
        exclude: list[str] | None = None,
    ) -> AgentProfile | None:
        """Select the best available agent from the registry.

        Filtering criteria:
        * *role* -- must match exactly if provided.
        * *min_level* -- agent level must be >= this value.
        * *exclude* -- agent IDs to skip.

        Among matching agents the one with the **lowest load** is
        returned.  Returns ``None`` if no agent matches.
        """
        exclude_set = set(exclude) if exclude else set()
        candidates = await self._registry.find_available(
            role=role,
            min_level=min_level,
        )
        candidates = [a for a in candidates if a.agent_id not in exclude_set]

        if not candidates:
            return None

        # Pick the agent with the lowest current load.
        return min(candidates, key=lambda a: a.load)

    # ------------------------------------------------------------------
    # Stage advancement
    # ------------------------------------------------------------------

    async def advance_task_stage(
        self,
        task_id: str,
        new_stage: TaskStage,
    ) -> TaskState:
        """Advance a task to *new_stage* and log the decision."""
        task = await self._task_core.update_stage(task_id, new_stage)

        await self._obs.log(
            event_type="dispatch_decision",
            source="dispatcher",
            details={
                "action": "advance_task_stage",
                "task_id": task_id,
                "new_stage": new_stage.value,
            },
            task_id=task_id,
        )

        return task

    # ------------------------------------------------------------------
    # Scheduler tick
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Periodic scheduler tick.

        Publishes a ``SCHEDULER_TICK`` message and inspects active tasks
        for automatic stage advancement:

        * Tasks stuck in ``PENDING`` with an assigned owner are moved to
          ``DISPATCHED``.
        * Tasks stuck in ``PLANNING`` with no owner are assigned the
          ``DISPATCHED`` stage (they will be picked up by the next
          assignment cycle).
        """
        tick_msg = Message.create(
            msg_type=MessageType.SCHEDULER_TICK,
            sender="dispatcher",
            payload={"tick": True},
        )
        await self._bus.publish(tick_msg)

        active_tasks = await self._task_core.get_active_tasks()

        for task in active_tasks:
            # PENDING tasks that already have an owner should move to DISPATCHED.
            if task.stage == TaskStage.PENDING and task.owner is not None:
                await self._task_core.update_stage(task.task_id, TaskStage.DISPATCHED)
                await self._obs.log(
                    event_type="dispatch_decision",
                    source="dispatcher.tick",
                    details={
                        "action": "auto_advance",
                        "task_id": task.task_id,
                        "from_stage": TaskStage.PENDING.value,
                        "to_stage": TaskStage.DISPATCHED.value,
                    },
                    task_id=task.task_id,
                )
            # PLANNING tasks without an owner get moved along so they are
            # eligible for assignment.
            elif task.stage == TaskStage.PLANNING and task.owner is None:
                await self._task_core.update_stage(task.task_id, TaskStage.DISPATCHED)
                await self._obs.log(
                    event_type="dispatch_decision",
                    source="dispatcher.tick",
                    details={
                        "action": "auto_advance",
                        "task_id": task.task_id,
                        "from_stage": TaskStage.PLANNING.value,
                        "to_stage": TaskStage.DISPATCHED.value,
                    },
                    task_id=task.task_id,
                )
