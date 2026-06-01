"""Feedback Attribution Layer (反馈归因层).

Converts human feedback into structured responsibility assignments,
preventing the "everyone takes the blame" problem in multi-agent
collaboration by mapping feedback to specific agents with weighted
responsibility.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.foundation.task_state import TaskStateCore
from cgc.models import FeedbackAttribution, FeedbackCategory

# ---------------------------------------------------------------------------
# Weight constants for auto-attribution
# ---------------------------------------------------------------------------

_OWNER_WEIGHT: float = 0.4
_PARTICIPANT_POOL_WEIGHT: float = 0.6


class FeedbackAttributionLayer:
    """Manage feedback attribution across agents in the CGC system.

    When human feedback is provided, this layer determines which agents
    bear responsibility and to what degree.  It supports both explicit
    (caller-specified) and automatic (participation-based) attribution.

    Parameters
    ----------
    task_core:
        The shared task-state store used to look up participants and owners.
    registry:
        The agent registry for resolving agent identities.
    observability:
        The append-only observability log for recording attribution events.
    """

    def __init__(
        self,
        task_core: TaskStateCore,
        registry: AgentRegistry,
        observability: ObservabilityLog,
    ) -> None:
        self._task_core = task_core
        self._registry = registry
        self._observability = observability

        self.attributions: list[FeedbackAttribution] = []
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Core attribution
    # ------------------------------------------------------------------

    async def attribute_feedback(
        self,
        task_id: str,
        category: FeedbackCategory,
        description: str,
        responsible_agents: list[dict[str, Any]] | None = None,
    ) -> list[FeedbackAttribution]:
        """Create feedback attributions for a task.

        Parameters
        ----------
        task_id:
            The task the feedback pertains to.
        category:
            The feedback category classification.
        description:
            Human-readable description of the feedback.
        responsible_agents:
            Optional explicit list of ``{"agent_id": str, "weight": float}``
            dicts.  When provided, attributions are created directly from
            these entries.  When *None*, the system auto-attributes based
            on task participation.

        Returns
        -------
        A list of :class:`FeedbackAttribution` records (one per responsible
        agent).
        """
        if responsible_agents is not None:
            created = await self._attribute_explicit(
                task_id=task_id,
                category=category,
                description=description,
                responsible_agents=responsible_agents,
            )
        else:
            created = await self._attribute_auto(
                task_id=task_id,
                category=category,
                description=description,
            )

        async with self._lock:
            self.attributions.extend(created)

        # Log each attribution to observability
        for attr in created:
            await self._observability.log(
                event_type="feedback_attributed",
                source="FeedbackAttributionLayer",
                agent_id=attr.agent_id,
                task_id=task_id,
                details={
                    "feedback_id": attr.feedback_id,
                    "category": category.value,
                    "responsibility_weight": attr.responsibility_weight,
                    "description": description,
                },
            )

        return created

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def get_attributions(
        self,
        task_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[FeedbackAttribution]:
        """Return attributions filtered by task and/or agent.

        When both *task_id* and *agent_id* are provided, the result is
        the intersection (attributions for that agent on that task).
        When neither is provided, all attributions are returned.
        """
        async with self._lock:
            results = list(self.attributions)

        if task_id is not None:
            results = [a for a in results if a.task_id == task_id]
        if agent_id is not None:
            results = [a for a in results if a.agent_id == agent_id]

        return results

    async def get_agent_attribution_score(self, agent_id: str) -> float:
        """Compute the aggregate attribution score for an agent.

        Returns a float in ``[0.0, 1.0]`` where:
        - ``0.0`` means no blame / no attributions
        - ``1.0`` means maximum blame

        The score is the mean of all responsibility weights for the agent.
        If the agent has no attributions, returns ``0.0``.
        """
        async with self._lock:
            agent_attrs = [
                a for a in self.attributions if a.agent_id == agent_id
            ]

        if not agent_attrs:
            return 0.0

        total_weight = sum(a.responsibility_weight for a in agent_attrs)
        # Clamp to [0.0, 1.0]
        score = min(1.0, total_weight / len(agent_attrs))
        return max(0.0, score)

    async def get_recent_feedback(self, limit: int = 20) -> list[FeedbackAttribution]:
        """Return the *limit* most recent attributions, newest first."""
        async with self._lock:
            tail = self.attributions[-limit:]

        # Return newest-first
        result = list(reversed(tail))
        return result

    # ------------------------------------------------------------------
    # Internal: explicit attribution
    # ------------------------------------------------------------------

    async def _attribute_explicit(
        self,
        task_id: str,
        category: FeedbackCategory,
        description: str,
        responsible_agents: list[dict[str, Any]],
    ) -> list[FeedbackAttribution]:
        """Create attributions from an explicit agent/weight list."""
        results: list[FeedbackAttribution] = []
        for entry in responsible_agents:
            agent_id = entry["agent_id"]
            weight = float(entry.get("weight", 1.0))
            # Clamp weight to [0.0, 1.0]
            weight = max(0.0, min(1.0, weight))

            attr = FeedbackAttribution.create(
                task_id=task_id,
                agent_id=agent_id,
                category=category,
                responsibility_weight=weight,
                description=description,
            )
            results.append(attr)
        return results

    # ------------------------------------------------------------------
    # Internal: auto-attribution
    # ------------------------------------------------------------------

    async def _attribute_auto(
        self,
        task_id: str,
        category: FeedbackCategory,
        description: str,
    ) -> list[FeedbackAttribution]:
        """Auto-attribute feedback based on task participation.

        Steps:
        1. Retrieve the task state.
        2. Collect all participants from the task.
        3. Identify the task owner (who receives higher weight).
        4. Distribute weights: owner gets 0.4, other participants
           share 0.6 equally.  If only the owner participated, they
           receive full weight (1.0).
        """
        task = await self._task_core.get_task(task_id)
        if task is None:
            # No task found -- cannot auto-attribute
            return []

        participants = list(task.participants)
        owner = task.owner

        # If no participants recorded, nothing to attribute
        if not participants and owner is None:
            return []

        # Build the agent list: owner + other participants
        other_agents = [p for p in participants if p != owner]

        results: list[FeedbackAttribution] = []

        if owner is not None:
            if not other_agents:
                # Owner is the sole participant -- full responsibility
                attr = FeedbackAttribution.create(
                    task_id=task_id,
                    agent_id=owner,
                    category=category,
                    responsibility_weight=1.0,
                    description=description,
                )
                results.append(attr)
            else:
                # Owner gets fixed weight
                owner_attr = FeedbackAttribution.create(
                    task_id=task_id,
                    agent_id=owner,
                    category=category,
                    responsibility_weight=_OWNER_WEIGHT,
                    description=description,
                )
                results.append(owner_attr)

                # Other participants split the pool equally
                per_agent = _PARTICIPANT_POOL_WEIGHT / len(other_agents)
                for agent_id in other_agents:
                    attr = FeedbackAttribution.create(
                        task_id=task_id,
                        agent_id=agent_id,
                        category=category,
                        responsibility_weight=round(per_agent, 6),
                        description=description,
                    )
                    results.append(attr)
        else:
            # No owner -- split equally among all participants
            if not participants:
                return []
            per_agent = 1.0 / len(participants)
            for agent_id in participants:
                attr = FeedbackAttribution.create(
                    task_id=task_id,
                    agent_id=agent_id,
                    category=category,
                    responsibility_weight=round(per_agent, 6),
                    description=description,
                )
                results.append(attr)

        return results
