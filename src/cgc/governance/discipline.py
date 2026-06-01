"""Discipline System for the CGC Governance layer.

Handles serious violations including scope breaches, forged results,
rule circumvention, and abnormal behavior patterns.  Produces
:class:`DisciplineSuggestion` records that drive WARN / FREEZE / DEMOTE /
RETIRE actions.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.models import (
    DisciplineAction,
    DisciplineReasonCode,
    DisciplineSuggestion,
)


# ---------------------------------------------------------------------------
# Metric-gate thresholds
# ---------------------------------------------------------------------------

_METRIC_GATES: list[dict[str, Any]] = [
    {
        "field": "human_correction_rate",
        "threshold": 0.4,
        "reason_code": DisciplineReasonCode.ABNORMAL_FAILURE_RATE,
        "label": "high human correction rate",
    },
    {
        "field": "rework_rate",
        "threshold": 0.5,
        "reason_code": DisciplineReasonCode.ABNORMAL_FAILURE_RATE,
        "label": "high failure / rework rate",
    },
    {
        "field": "downstream_breakage",
        "threshold": 0.3,
        "reason_code": DisciplineReasonCode.ABNORMAL_FAILURE_RATE,
        "label": "high downstream breakage rate",
    },
    {
        "field": "risk_tendency",
        "threshold": 0.6,
        "reason_code": DisciplineReasonCode.ABNORMAL_FAILURE_RATE,
        "label": "high risk event density",
    },
]


def _determine_action(
    violation_count: int,
    config: dict[str, int],
    *,
    severe: bool = False,
    critical: bool = False,
) -> DisciplineAction:
    """Decide the discipline action based on violation history and severity.

    Parameters
    ----------
    violation_count:
        Number of prior violations for this agent.
    config:
        Threshold configuration dict.
    severe:
        True when the trigger represents a severe violation (e.g. scope breach,
        forged receipt).
    critical:
        True when the trigger represents a critical / irrecoverable violation.
    """
    if critical:
        return DisciplineAction.RETIRE
    if severe or violation_count >= config["demote_threshold"]:
        return DisciplineAction.DEMOTE
    if violation_count >= config["auto_freeze_threshold"]:
        return DisciplineAction.FREEZE
    return DisciplineAction.WARN


class DisciplineSystem:
    """Evaluate discipline triggers and maintain violation history.

    Parameters
    ----------
    registry:
        The agent registry used to look up profiles and reputations.
    observability:
        The append-only observability log for recording suggestions.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        observability: ObservabilityLog,
    ) -> None:
        self._registry = registry
        self._observability = observability
        self.suggestions: list[DisciplineSuggestion] = []
        self.violation_counts: dict[str, int] = {}
        self.config: dict[str, int] = {
            "auto_freeze_threshold": 3,
            "warning_threshold": 1,
            "demote_threshold": 5,
        }
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    async def evaluate_trigger(
        self,
        agent_id: str,
        trigger_type: str,
        evidence: list[str],
    ) -> DisciplineSuggestion | None:
        """Evaluate a single discipline trigger.

        Parameters
        ----------
        agent_id:
            The agent that triggered the evaluation.
        trigger_type:
            Either ``"metric_gate"`` or ``"behavior_gate"``.
        evidence:
            Human- or machine-readable strings supporting the trigger.

        Returns
        -------
        A :class:`DisciplineSuggestion` if a violation is detected, otherwise
        ``None``.
        """
        async with self._lock:
            count = self.violation_counts.get(agent_id, 0)

            if trigger_type == "metric_gate":
                suggestion = await self._evaluate_metric_trigger(
                    agent_id, evidence, count,
                )
            elif trigger_type == "behavior_gate":
                suggestion = await self._evaluate_behavior_trigger(
                    agent_id, evidence, count,
                )
            else:
                return None

            if suggestion is not None:
                self.violation_counts[agent_id] = count + 1

            return suggestion

    # ------------------------------------------------------------------
    # Metric gates
    # ------------------------------------------------------------------

    async def check_metric_gates(
        self,
        agent_id: str,
    ) -> list[DisciplineSuggestion]:
        """Check the agent's reputation metrics against defined thresholds.

        Inspects ``human_correction_rate``, ``rework_rate``,
        ``downstream_breakage``, and ``risk_tendency`` via the agent
        profile metadata (``reputation`` key).

        Returns a :class:`DisciplineSuggestion` for every metric that
        exceeds its threshold.
        """
        agent = await self._registry.get(agent_id)
        if agent is None:
            return []

        reputation: dict[str, Any] = agent.metadata.get("reputation", {})
        results: list[DisciplineSuggestion] = []

        async with self._lock:
            count = self.violation_counts.get(agent_id, 0)

            for gate in _METRIC_GATES:
                value = reputation.get(gate["field"], 0.0)
                if value > gate["threshold"]:
                    action = _determine_action(count + len(results), self.config)
                    suggestion = DisciplineSuggestion.create(
                        agent_id=agent_id,
                        action=action,
                        reason_code=gate["reason_code"],
                        severity="medium",
                        evidence=[
                            f"{gate['label']}: {gate['field']}={value:.3f} "
                            f"(threshold={gate['threshold']})"
                        ],
                    )
                    results.append(suggestion)

        return results

    # ------------------------------------------------------------------
    # Behavior gates
    # ------------------------------------------------------------------

    async def check_behavior_gates(
        self,
        agent_id: str,
        recent_events: list[dict[str, Any]],
    ) -> list[DisciplineSuggestion]:
        """Analyze recent events for behavioral violations.

        Detects scope breaches, illegal capability combos,
        high-frequency bypasses, and forged receipts from the
        provided event list.

        Parameters
        ----------
        agent_id:
            The agent under scrutiny.
        recent_events:
            A list of event dicts (as returned by
            :class:`ObservabilityLog`).
        """
        results: list[DisciplineSuggestion] = []

        async with self._lock:
            count = self.violation_counts.get(agent_id, 0)

            scope_breach_evidence: list[str] = []
            forged_evidence: list[str] = []
            bypass_evidence: list[str] = []
            capability_bypass_evidence: list[str] = []

            for event in recent_events:
                event_type = event.get("event_type", "")
                details = event.get("details", {})

                if event_type == "scope_breach":
                    scope_breach_evidence.append(
                        f"Scope breach at {event.get('timestamp')}: "
                        f"{details.get('description', 'no description')}"
                    )

                if event_type == "forged_receipt":
                    forged_evidence.append(
                        f"Forged result at {event.get('timestamp')}: "
                        f"{details.get('description', 'no description')}"
                    )

                if event_type == "capability_bypass":
                    capability_bypass_evidence.append(
                        f"Capability assembly bypass at {event.get('timestamp')}: "
                        f"{details.get('description', 'no description')}"
                    )

                if event_type == "high_frequency_bypass":
                    freq = details.get("frequency", 0)
                    bypass_evidence.append(
                        f"High frequency bypass at {event.get('timestamp')}: "
                        f"frequency={freq}"
                    )

            # -- Scope breach (severe) ------------------------------------
            if scope_breach_evidence:
                action = _determine_action(count + len(results), self.config, severe=True)
                suggestion = DisciplineSuggestion.create(
                    agent_id=agent_id,
                    action=action,
                    reason_code=DisciplineReasonCode.SCOPE_BREACH,
                    severity="high",
                    suggested_duration="24h" if action == DisciplineAction.FREEZE else None,
                    evidence=scope_breach_evidence,
                )
                results.append(suggestion)

            # -- Forged result (critical) ---------------------------------
            if forged_evidence:
                action = _determine_action(
                    count + len(results), self.config, critical=True,
                )
                suggestion = DisciplineSuggestion.create(
                    agent_id=agent_id,
                    action=action,
                    reason_code=DisciplineReasonCode.FORGED_RESULT,
                    severity="critical",
                    evidence=forged_evidence,
                )
                results.append(suggestion)

            # -- Capability assembly bypass (severe) -----------------------
            if capability_bypass_evidence:
                action = _determine_action(count + len(results), self.config, severe=True)
                suggestion = DisciplineSuggestion.create(
                    agent_id=agent_id,
                    action=action,
                    reason_code=DisciplineReasonCode.CAPABILITY_ASSEMBLY_BYPASS,
                    severity="high",
                    evidence=capability_bypass_evidence,
                )
                results.append(suggestion)

            # -- High frequency bypass ------------------------------------
            if bypass_evidence:
                action = _determine_action(count + len(results), self.config)
                suggestion = DisciplineSuggestion.create(
                    agent_id=agent_id,
                    action=action,
                    reason_code=DisciplineReasonCode.HIGH_FREQUENCY_BYPASS,
                    severity="medium",
                    evidence=bypass_evidence,
                )
                results.append(suggestion)

        return results

    # ------------------------------------------------------------------
    # Recording / retrieval
    # ------------------------------------------------------------------

    async def record_suggestion(self, suggestion: DisciplineSuggestion) -> None:
        """Record a suggestion and emit an observability log entry."""
        async with self._lock:
            self.suggestions.append(suggestion)
            self.violation_counts[suggestion.agent_id] = (
                self.violation_counts.get(suggestion.agent_id, 0) + 1
            )

        await self._observability.log(
            event_type="discipline_suggestion",
            source="DisciplineSystem",
            agent_id=suggestion.agent_id,
            details={
                "action": suggestion.action.value,
                "reason_code": suggestion.reason_code.value,
                "severity": suggestion.severity,
                "evidence": suggestion.evidence,
            },
        )

    async def get_suggestions(
        self,
        agent_id: str | None = None,
    ) -> list[DisciplineSuggestion]:
        """Return recorded suggestions, optionally filtered by agent."""
        async with self._lock:
            if agent_id is None:
                return list(self.suggestions)
            return [s for s in self.suggestions if s.agent_id == agent_id]

    async def get_violation_count(self, agent_id: str) -> int:
        """Return the total number of recorded violations for *agent_id*."""
        async with self._lock:
            return self.violation_counts.get(agent_id, 0)

    async def should_auto_freeze(self, agent_id: str) -> bool:
        """Return ``True`` when the violation count meets or exceeds the
        auto-freeze threshold."""
        count = await self.get_violation_count(agent_id)
        return count >= self.config["auto_freeze_threshold"]

    # ------------------------------------------------------------------
    # Internal helpers (must be called under self._lock)
    # ------------------------------------------------------------------

    async def _evaluate_metric_trigger(
        self,
        agent_id: str,
        evidence: list[str],
        current_count: int,
    ) -> DisciplineSuggestion | None:
        action = _determine_action(current_count, self.config)
        return DisciplineSuggestion.create(
            agent_id=agent_id,
            action=action,
            reason_code=DisciplineReasonCode.ABNORMAL_FAILURE_RATE,
            severity="medium",
            evidence=evidence,
        )

    async def _evaluate_behavior_trigger(
        self,
        agent_id: str,
        evidence: list[str],
        current_count: int,
    ) -> DisciplineSuggestion | None:
        if not evidence:
            return None

        evidence_text = " ".join(evidence).lower()

        # Determine reason code from evidence content
        severe = False
        critical = False
        reason_code = DisciplineReasonCode.REPEATED_VIOLATION

        if "scope_breach" in evidence_text or "scope breach" in evidence_text:
            reason_code = DisciplineReasonCode.SCOPE_BREACH
            severe = True
        elif "forged" in evidence_text or "forgery" in evidence_text:
            reason_code = DisciplineReasonCode.FORGED_RESULT
            critical = True
        elif "bypass" in evidence_text and "capability" in evidence_text:
            reason_code = DisciplineReasonCode.CAPABILITY_ASSEMBLY_BYPASS
            severe = True
        elif "bypass" in evidence_text or "high_frequency" in evidence_text:
            reason_code = DisciplineReasonCode.HIGH_FREQUENCY_BYPASS
        elif current_count > 0:
            reason_code = DisciplineReasonCode.REPEATED_VIOLATION

        action = _determine_action(
            current_count, self.config, severe=severe, critical=critical,
        )
        severity = "critical" if critical else ("high" if severe else "medium")

        return DisciplineSuggestion.create(
            agent_id=agent_id,
            action=action,
            reason_code=reason_code,
            severity=severity,
            suggested_duration="24h" if action == DisciplineAction.FREEZE else None,
            evidence=evidence,
        )
