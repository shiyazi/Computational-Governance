"""Elevation Engine for the CGC Governance layer.

Handles agent level changes: promotion, demotion, observation periods,
and dynamic tightening of the high-level agent ratio.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.models import (
    AgentLevel,
    AgentProfile,
    AgentStatus,
    ElevationDecision,
    ElevationRequest,
)
from cgc.governance.reputation import ReputationRating


class ElevationEngine:
    """Manages agent elevation (promotion / demotion) decisions.

    Phase 1 -- *check_eligibility* performs hard-gate metric checks
    against the agent's reputation profile.

    Phase 2 -- *evaluate* runs a committee-style evaluation that
    considers the high-level agent ratio, promotion window status,
    and observation-period completion before approving or denying a
    request.

    Parameters
    ----------
    registry:
        Agent registry for looking up and mutating agent profiles.
    reputation:
        Reputation rating store for reading metric profiles.
    observability:
        Observability log for recording elevation events.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        reputation: ReputationRating,
        observability: ObservabilityLog,
    ) -> None:
        self._registry = registry
        self._reputation = reputation
        self._observability = observability

        self.pending_requests: list[ElevationRequest] = []
        self.decisions: list[ElevationDecision] = []
        self.config: dict[str, Any] = {
            "high_level_ratio_threshold": 0.3,
            "promotion_window_open": True,
            "observation_periods": {},  # agent_id -> epoch timestamp when period ends
            # Dynamic gate thresholds (Section 11.3: can be raised dynamically)
            "gate_thresholds": {
                "completion_rate": 0.8,
                "first_pass_acceptance": 0.7,
                "rework_rate": 0.2,
                "downstream_breakage": 0.1,
                "human_correction_rate": 0.15,
                "near_miss_count": 3,  # max allowed near-miss events
            },
            "gate_threshold_level": 0,  # 0=base, 1=raised, 2=high, 3=strict
        }

        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Phase 1 -- Hard-gate eligibility check
    # ------------------------------------------------------------------

    async def check_eligibility(self, agent_id: str) -> dict[str, Any]:
        """Run hard-gate metric checks against the agent's reputation profile.

        Phase 1 hard gates (Section 11.2):
        - completion_rate > threshold
        - first_pass_acceptance > threshold
        - rework_rate < threshold
        - downstream_breakage < threshold
        - human_correction_rate < threshold
        - discipline_events == 0
        - near_miss_count <= threshold

        Returns
        -------
        dict
            ``eligible`` (bool), ``metrics`` (dict of metric values),
            ``failed_gates`` (list of gate names that were not met).
        """
        profile = await self._reputation.get_profile(agent_id)

        metrics: dict[str, Any] = {}
        failed_gates: list[str] = []

        if profile is None:
            # No profile means the agent has no track record yet.
            return {
                "eligible": False,
                "metrics": metrics,
                "failed_gates": ["no_reputation_profile"],
            }

        # Read current (dynamically adjusted) gate thresholds
        gates = self.config["gate_thresholds"]

        # Extract metrics from profile.
        metrics = {
            "completion_rate": profile.completion_rate,
            "first_pass_acceptance": profile.first_pass_acceptance,
            "rework_rate": profile.rework_rate,
            "downstream_breakage": profile.downstream_breakage,
            "human_correction_rate": profile.human_correction_rate,
        }

        # Discipline events come from the reputation store.
        discipline_events = await self._reputation.get_discipline_event_count(
            agent_id
        )
        metrics["discipline_events"] = discipline_events

        # Near-miss count from observability (high-risk events that almost caused issues)
        near_miss_logs = await self._observability.query(
            event_type="near_miss",
            agent_id=agent_id,
            limit=1000,
        )
        near_miss_count = len(near_miss_logs)
        metrics["near_miss_count"] = near_miss_count

        # Hard gates using dynamic thresholds.
        if profile.completion_rate <= gates["completion_rate"]:
            failed_gates.append("completion_rate")
        if profile.first_pass_acceptance <= gates["first_pass_acceptance"]:
            failed_gates.append("first_pass_acceptance")
        if profile.rework_rate >= gates["rework_rate"]:
            failed_gates.append("rework_rate")
        if profile.downstream_breakage >= gates["downstream_breakage"]:
            failed_gates.append("downstream_breakage")
        if profile.human_correction_rate >= gates["human_correction_rate"]:
            failed_gates.append("human_correction_rate")
        if discipline_events != 0:
            failed_gates.append("discipline_events")
        if near_miss_count > gates["near_miss_count"]:
            failed_gates.append("near_miss_count")

        await self._observability.log(
            event_type="elevation_eligibility_check",
            source="ElevationEngine",
            details={
                "agent_id": agent_id,
                "eligible": len(failed_gates) == 0,
                "failed_gates": failed_gates,
                "metrics": metrics,
                "gate_threshold_level": self.config["gate_threshold_level"],
            },
            agent_id=agent_id,
        )

        return {
            "eligible": len(failed_gates) == 0,
            "metrics": metrics,
            "failed_gates": failed_gates,
        }

    # ------------------------------------------------------------------
    # Request creation
    # ------------------------------------------------------------------

    async def request_elevation(
        self,
        agent_id: str,
        target_level: AgentLevel,
    ) -> ElevationRequest:
        """Create and store an elevation request if the agent is eligible.

        Raises
        ------
        ValueError
            If the agent is not eligible for elevation.
        """
        eligibility = await self.check_eligibility(agent_id)
        if not eligibility["eligible"]:
            raise ValueError(
                f"Agent '{agent_id}' is not eligible for elevation. "
                f"Failed gates: {eligibility['failed_gates']}"
            )

        request = ElevationRequest.create(
            agent_id=agent_id,
            target_level=target_level,
            metrics=eligibility["metrics"],
        )

        async with self._lock:
            self.pending_requests.append(request)

        await self._observability.log(
            event_type="elevation_request_created",
            source="ElevationEngine",
            details={
                "agent_id": agent_id,
                "target_level": target_level.name,
            },
            agent_id=agent_id,
        )

        return request

    # ------------------------------------------------------------------
    # Phase 2 -- Committee evaluation
    # ------------------------------------------------------------------

    async def evaluate(self, request: ElevationRequest) -> ElevationDecision:
        """Run a committee-style evaluation on an elevation request.

        Phase 2 committee evaluation (Section 11.2).  Inputs include:
        1. Agent reputation profile.
        2. High-level ratio has not exceeded the threshold.
        3. Promotion window is open.
        4. Observation period (if any) has completed.
        5. Human feedback attribution results (Section 15).
        6. Governance history (past elevation/discipline/constitution events).

        Returns an :class:`ElevationDecision`.
        """
        committee_scores: dict[str, Any] = {}
        deny_reasons: list[str] = []

        # 1 -- reputation profile
        profile = await self._reputation.get_profile(request.agent_id)
        if profile is None:
            deny_reasons.append("no_reputation_profile")
            committee_scores["profile_check"] = False
        else:
            committee_scores["profile_check"] = True
            committee_scores["profile_long_term_score"] = profile.long_term_score
            committee_scores["profile_maturity_score"] = profile.maturity_score

        # 2 -- high-level ratio
        ratio = await self.check_high_level_ratio()
        committee_scores["high_level_ratio"] = ratio
        if ratio >= self.config["high_level_ratio_threshold"]:
            deny_reasons.append("high_level_ratio_exceeded")
            committee_scores["ratio_check"] = False
        else:
            committee_scores["ratio_check"] = True

        # 3 -- promotion window
        window_open = self.config["promotion_window_open"]
        committee_scores["promotion_window_open"] = window_open
        if not window_open:
            deny_reasons.append("promotion_window_closed")
            committee_scores["window_check"] = False
        else:
            committee_scores["window_check"] = True

        # 4 -- observation period
        obs_end = self.config["observation_periods"].get(request.agent_id)
        if obs_end is not None:
            period_complete = time.time() >= obs_end
            committee_scores["observation_period_complete"] = period_complete
            if not period_complete:
                deny_reasons.append("observation_period_incomplete")
        else:
            committee_scores["observation_period_complete"] = True

        # 5 -- Human feedback attribution results (Section 11.2 input)
        feedback_logs = await self._observability.query(
            event_type="human_feedback",
            agent_id=request.agent_id,
            limit=100,
        )
        feedback_count = len(feedback_logs)
        avg_feedback_weight = 0.0
        if feedback_count > 0:
            weights = [
                entry.get("details", {}).get("weight", 0.0)
                for entry in feedback_logs
                if isinstance(entry.get("details"), dict)
            ]
            avg_feedback_weight = sum(weights) / len(weights) if weights else 0.0
        committee_scores["human_feedback_count"] = feedback_count
        committee_scores["human_feedback_avg_weight"] = avg_feedback_weight
        # Deny if agent has high feedback burden
        if avg_feedback_weight > 0.5 and feedback_count > 3:
            deny_reasons.append("high_feedback_burden")

        # 6 -- Governance history (past elevation/discipline/constitution events)
        gov_events = await self._observability.query(
            agent_id=request.agent_id,
            limit=200,
        )
        past_denials = sum(
            1 for e in gov_events
            if isinstance(e.get("details"), dict)
            and e.get("event_type") == "elevation_decision"
            and not e["details"].get("approved", True)
        )
        past_discipline = sum(
            1 for e in gov_events
            if e.get("event_type") in ("discipline_suggestion", "discipline_event")
        )
        committee_scores["past_elevation_denials"] = past_denials
        committee_scores["past_discipline_events"] = past_discipline
        # Deny if agent has recent elevation denials
        if past_denials >= 3:
            deny_reasons.append("repeated_elevation_denials")

        approved = len(deny_reasons) == 0
        reason = "; ".join(deny_reasons) if deny_reasons else "All checks passed."

        decision = ElevationDecision.create(
            request=request,
            approved=approved,
            reason=reason,
            committee_scores=committee_scores,
        )

        async with self._lock:
            self.decisions.append(decision)

        # If approved, update the agent's level in the registry.
        if approved:
            await self._registry.update_level(request.agent_id, request.target_level)
            # Clear any observation period for this agent.
            async with self._lock:
                self.config["observation_periods"].pop(request.agent_id, None)

        await self._observability.log(
            event_type="elevation_decision",
            source="ElevationEngine",
            details={
                "agent_id": request.agent_id,
                "target_level": request.target_level.name,
                "approved": approved,
                "reason": reason,
                "committee_scores": committee_scores,
            },
            agent_id=request.agent_id,
        )

        return decision

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    async def process_pending(self) -> list[ElevationDecision]:
        """Evaluate and resolve all pending elevation requests.

        Returns the list of decisions produced.
        """
        async with self._lock:
            requests = list(self.pending_requests)
            self.pending_requests.clear()

        results: list[ElevationDecision] = []
        for request in requests:
            decision = await self.evaluate(request)
            results.append(decision)

        await self._observability.log(
            event_type="elevation_batch_processed",
            source="ElevationEngine",
            details={
                "request_count": len(requests),
                "approved_count": sum(1 for d in results if d.approved),
                "denied_count": sum(1 for d in results if not d.approved),
            },
        )

        return results

    # ------------------------------------------------------------------
    # Ratio management
    # ------------------------------------------------------------------

    async def check_high_level_ratio(self) -> float:
        """Return the ratio of SENIOR+ agents to total active agents."""
        agents = await self._registry.find_available()
        total = len(agents)
        if total == 0:
            return 0.0

        high_level_count = sum(
            1 for a in agents if a.level >= AgentLevel.SENIOR
        )
        return high_level_count / total

    async def tighten_promotion(self) -> None:
        """If the high-level ratio exceeds the threshold, raise gates
        and/or close the promotion window (Section 11.3).

        Dynamic tightening levels:
        - Level 0 (base): default thresholds
        - Level 1 (raised): +10% on rate thresholds, -10% on violation thresholds
        - Level 2 (high): +20% on rate thresholds, close window for non-PRINCIPAL
        - Level 3 (strict): window fully closed, highest thresholds
        """
        ratio = await self.check_high_level_ratio()
        threshold = self.config["high_level_ratio_threshold"]

        if ratio < threshold:
            return

        async with self._lock:
            level = self.config["gate_threshold_level"]
            level = min(level + 1, 3)
            self.config["gate_threshold_level"] = level

            base = {
                "completion_rate": 0.8,
                "first_pass_acceptance": 0.7,
                "rework_rate": 0.2,
                "downstream_breakage": 0.1,
                "human_correction_rate": 0.15,
                "near_miss_count": 3,
            }

            if level == 1:
                # Raise gates by 10%
                self.config["gate_thresholds"] = {
                    "completion_rate": 0.88,
                    "first_pass_acceptance": 0.77,
                    "rework_rate": 0.18,
                    "downstream_breakage": 0.09,
                    "human_correction_rate": 0.135,
                    "near_miss_count": 2,
                }
            elif level == 2:
                # Raise gates by 20%
                self.config["gate_thresholds"] = {
                    "completion_rate": 0.92,
                    "first_pass_acceptance": 0.82,
                    "rework_rate": 0.15,
                    "downstream_breakage": 0.07,
                    "human_correction_rate": 0.10,
                    "near_miss_count": 1,
                }
                # Close window for anything below PRINCIPAL
                self.config["promotion_window_open"] = False
            elif level >= 3:
                # Strict: window fully closed, highest thresholds
                self.config["gate_thresholds"] = {
                    "completion_rate": 0.95,
                    "first_pass_acceptance": 0.88,
                    "rework_rate": 0.10,
                    "downstream_breakage": 0.05,
                    "human_correction_rate": 0.08,
                    "near_miss_count": 0,
                }
                self.config["promotion_window_open"] = False

        await self._observability.log(
            event_type="elevation_window_tightened",
            source="ElevationEngine",
            details={
                "high_level_ratio": ratio,
                "threshold": threshold,
                "window_open": self.config["promotion_window_open"],
                "gate_threshold_level": level,
                "gate_thresholds": self.config["gate_thresholds"],
            },
        )

    async def open_promotion_window(self) -> None:
        """Reopen the promotion window and reset gate thresholds to base."""
        async with self._lock:
            self.config["promotion_window_open"] = True
            self.config["gate_threshold_level"] = 0
            self.config["gate_thresholds"] = {
                "completion_rate": 0.8,
                "first_pass_acceptance": 0.7,
                "rework_rate": 0.2,
                "downstream_breakage": 0.1,
                "human_correction_rate": 0.15,
                "near_miss_count": 3,
            }

        await self._observability.log(
            event_type="elevation_window_opened",
            source="ElevationEngine",
            details={"gate_threshold_level": 0},
        )

    # ------------------------------------------------------------------
    # Demotion
    # ------------------------------------------------------------------

    async def demote(self, agent_id: str, reason: str) -> ElevationDecision:
        """Immediately demote an agent by one level.

        Parameters
        ----------
        agent_id:
            The agent to demote.
        reason:
            Human-readable reason for the demotion.

        Returns an :class:`ElevationDecision` recording the action.
        """
        agent = await self._registry.get(agent_id)
        if agent is None:
            raise ValueError(f"Agent not found: {agent_id}")

        # Demote by one level, but never below NOVICE.
        current = agent.level
        new_level = AgentLevel(max(AgentLevel.NOVICE.value, current.value - 1))

        request = ElevationRequest.create(
            agent_id=agent_id,
            target_level=new_level,
            metrics={"demotion_reason": reason, "previous_level": current.name},
        )

        # Build decision -- demotion is always approved.
        committee_scores: dict[str, Any] = {
            "previous_level": current.name,
            "new_level": new_level.name,
            "demotion": True,
        }

        decision = ElevationDecision.create(
            request=request,
            approved=True,
            reason=f"Demotion: {reason}",
            committee_scores=committee_scores,
        )

        await self._registry.update_level(agent_id, new_level)

        async with self._lock:
            self.decisions.append(decision)

        await self._observability.log(
            event_type="elevation_demotion",
            source="ElevationEngine",
            details={
                "agent_id": agent_id,
                "previous_level": current.name,
                "new_level": new_level.name,
                "reason": reason,
            },
            agent_id=agent_id,
        )

        return decision

    # ------------------------------------------------------------------
    # Observation periods
    # ------------------------------------------------------------------

    async def set_observation_period(
        self, agent_id: str, duration_days: int
    ) -> None:
        """Set an observation period for *agent_id* of *duration_days*.

        The observation period must complete before the agent can be
        promoted.  Internally stored as the epoch timestamp at which
        the period ends.
        """
        end_time = time.time() + (duration_days * 86400)

        async with self._lock:
            self.config["observation_periods"][agent_id] = end_time

        # Also set the agent status to OBSERVATION.
        try:
            await self._registry.update_status(agent_id, AgentStatus.OBSERVATION)
        except KeyError:
            pass  # agent may not be registered; still record the period

        await self._observability.log(
            event_type="elevation_observation_period_set",
            source="ElevationEngine",
            details={
                "agent_id": agent_id,
                "duration_days": duration_days,
                "ends_at": end_time,
            },
            agent_id=agent_id,
        )

    # ------------------------------------------------------------------
    # History and query
    # ------------------------------------------------------------------

    async def get_elevation_history(
        self, agent_id: str
    ) -> list[ElevationDecision]:
        """Return all elevation decisions for *agent_id*."""
        async with self._lock:
            return [
                d
                for d in self.decisions
                if d.request.agent_id == agent_id
            ]

    async def get_pending(self) -> list[ElevationRequest]:
        """Return a snapshot of the current pending requests."""
        async with self._lock:
            return list(self.pending_requests)
