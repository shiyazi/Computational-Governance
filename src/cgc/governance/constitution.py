"""Constitution Engine for the CGC Governance layer.

Handles appeals and reviews -- not daily approvals.  Reviews cover
promotion denials, discipline suggestions, excessive punishment,
procedural errors, and capability restriction disputes.

Each appeal proceeds through a three-part review:
1. Procedural correctness of the original decision.
2. Evidence sufficiency.
3. Proportionality of the outcome.

The engine then produces a :class:`ConstitutionRuling` with one of
four verdicts: UPHELD, OVERTURNED, RESCINDED, or REMANDED.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.models import (
    ConstitutionAppeal,
    ConstitutionRuling,
    ConstitutionVerdict,
)


# ---------------------------------------------------------------------------
# Review weights and thresholds
# ---------------------------------------------------------------------------

_REVIEW_CRITERIA: dict[str, float] = {
    "procedural_correctness": 0.4,
    "evidence_sufficiency": 0.35,
    "proportionality": 0.25,
}

_PROPORTIONALITY_SEVERITY_MAP: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assess_procedural_correctness(
    contested_entity_type: str,
    evidence: list[str],
) -> tuple[bool, str]:
    """Check whether the original decision followed due process.

    Returns (correct, detail).
    """
    entity = contested_entity_type.lower()

    if entity in ("discipline", "discipline_suggestion"):
        # Discipline decisions require at least one piece of evidence
        # and a documented trigger.
        has_evidence = len(evidence) > 0
        has_trigger = any("trigger" in e.lower() for e in evidence)
        if not has_evidence:
            return False, "No evidence was provided for the discipline action."
        if not has_trigger:
            return False, "No documented trigger type for the discipline action."
        return True, "Procedural requirements for discipline met."

    if entity in ("elevation", "elevation_denial", "promotion_denial"):
        # Elevation denials require eligibility metrics and committee scores.
        has_metrics = any("metric" in e.lower() for e in evidence)
        has_committee = any(
            "committee" in e.lower() or "score" in e.lower() for e in evidence
        )
        if not has_metrics and not has_committee:
            return False, "No eligibility metrics or committee scores recorded."
        return True, "Procedural requirements for elevation review met."

    if entity in ("capability", "capability_restriction"):
        # Capability restrictions require a stated rule reference.
        has_rule = any("rule" in e.lower() or "policy" in e.lower() for e in evidence)
        if not has_rule:
            return False, "No rule or policy reference provided for capability restriction."
        return True, "Procedural requirements for capability restriction met."

    # Default for unknown entity types -- procedural if evidence exists.
    if evidence:
        return True, "Evidence provided; procedural baseline met."
    return False, "No evidence provided for the contested decision."


def _assess_evidence_sufficiency(
    evidence: list[str],
    contested_entity_type: str,
) -> tuple[bool, str]:
    """Evaluate whether the evidence is sufficient to support the decision."""
    if not evidence:
        return False, "No evidence submitted."

    entity = contested_entity_type.lower()

    if entity in ("discipline", "discipline_suggestion"):
        if len(evidence) < 2:
            return False, "Insufficient evidence: fewer than 2 supporting items."
        return True, "Sufficient evidence for discipline action."

    if entity in ("elevation", "elevation_denial", "promotion_denial"):
        if len(evidence) < 1:
            return False, "No evidence supporting the elevation denial."
        return True, "Sufficient evidence for elevation decision."

    if entity in ("capability", "capability_restriction"):
        if len(evidence) < 1:
            return False, "No evidence supporting the capability restriction."
        return True, "Sufficient evidence for capability restriction."

    # Default threshold.
    if len(evidence) >= 2:
        return True, "Evidence meets default sufficiency threshold."
    return False, "Insufficient evidence under default threshold."


def _assess_proportionality(
    contested_entity_type: str,
    grounds: str,
    evidence: list[str],
) -> tuple[bool, str]:
    """Evaluate whether the original outcome was proportional."""
    entity = contested_entity_type.lower()
    grounds_lower = grounds.lower()

    if entity in ("discipline", "discipline_suggestion"):
        # Check for severity keywords in the evidence.
        max_severity = 0
        for ev in evidence:
            ev_lower = ev.lower()
            for label, weight in _PROPORTIONALITY_SEVERITY_MAP.items():
                if label in ev_lower:
                    max_severity = max(max_severity, weight)

        # If grounds mention excessive or disproportionate, lean towards
        # a finding of disproportionality.
        if "excessive" in grounds_lower or "disproportionate" in grounds_lower:
            return False, "Appellant claims disproportionate response."

        if max_severity >= 4:
            return True, "Critical severity justifies strong action."
        if max_severity >= 3:
            return True, "High severity proportional to action."
        if max_severity <= 1:
            return False, "Low severity issue; outcome may be disproportionate."
        return True, "Outcome proportional to severity."

    if entity in ("elevation", "elevation_denial", "promotion_denial"):
        # Promotion denials are proportional unless evidence is very weak.
        if len(evidence) < 1:
            return False, "Denial lacks supporting justification."
        return True, "Denial proportional to stated reasons."

    if entity in ("capability", "capability_restriction"):
        if "unfair" in grounds_lower or "unjustified" in grounds_lower:
            return False, "Appellant claims unjustified capability restriction."
        return True, "Capability restriction proportional."

    return True, "Default proportionality assessment passed."


def _compute_verdict(
    procedural_correct: bool,
    evidence_sufficient: bool,
    proportional: bool,
    procedural_detail: str,
    evidence_detail: str,
    proportionality_detail: str,
) -> tuple[ConstitutionVerdict, str]:
    """Determine the verdict based on the three review axes.

    Returns (verdict, reasoning).
    """
    parts: list[str] = []

    if procedural_correct:
        parts.append(f"Procedural correctness: satisfied -- {procedural_detail}")
    else:
        parts.append(f"Procedural correctness: deficient -- {procedural_detail}")

    if evidence_sufficient:
        parts.append(f"Evidence sufficiency: satisfied -- {evidence_detail}")
    else:
        parts.append(f"Evidence sufficiency: deficient -- {evidence_detail}")

    if proportional:
        parts.append(f"Proportionality: satisfied -- {proportionality_detail}")
    else:
        parts.append(f"Proportionality: not satisfied -- {proportionality_detail}")

    # Decision matrix.
    if procedural_correct and evidence_sufficient and proportional:
        verdict = ConstitutionVerdict.UPHELD
    elif not procedural_correct and not evidence_sufficient:
        # Both process and evidence broken -- cancel entirely.
        verdict = ConstitutionVerdict.RESCINDED
    elif not procedural_correct:
        # Procedural error but evidence may be okay -- send back for redo.
        verdict = ConstitutionVerdict.REMANDED
    elif not proportional:
        # Process ok but outcome too harsh -- reverse.
        verdict = ConstitutionVerdict.OVERTURNED
    elif not evidence_sufficient:
        # Evidence gaps -- send back for a proper hearing.
        verdict = ConstitutionVerdict.REMANDED
    else:
        # Should not reach here, but default to upheld.
        verdict = ConstitutionVerdict.UPHELD

    reasoning = " | ".join(parts)
    return verdict, reasoning


# ---------------------------------------------------------------------------
# ConstitutionEngine
# ---------------------------------------------------------------------------


class ConstitutionEngine:
    """Handles appeals and constitutional reviews.

    Reviews are limited to: promotion denials, discipline suggestions,
    excessive punishment, procedural errors, and capability restriction
    disputes.  This engine does **not** handle daily approvals.

    Parameters
    ----------
    registry:
        Agent registry for looking up agent profiles.
    observability:
        Observability log for recording appeal and ruling events.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        observability: ObservabilityLog,
    ) -> None:
        self._registry = registry
        self._observability = observability

        self.appeals: list[ConstitutionAppeal] = []
        self.rulings: list[ConstitutionRuling] = []

        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Filing
    # ------------------------------------------------------------------

    async def file_appeal(
        self,
        appellant_id: str,
        contested_entity_id: str,
        contested_entity_type: str,
        grounds: str,
        evidence: list[str],
    ) -> ConstitutionAppeal:
        """File a new constitutional appeal.

        Parameters
        ----------
        appellant_id:
            The agent filing the appeal.
        contested_entity_id:
            Identifier of the entity being contested (e.g. a discipline
            suggestion id or elevation decision id).
        contested_entity_type:
            Category of the contested entity (e.g. ``"discipline"``,
            ``"elevation_denial"``, ``"capability_restriction"``).
        grounds:
            Human-readable explanation of why the appeal is being filed.
        evidence:
            Supporting evidence strings.

        Returns
        -------
        The newly created :class:`ConstitutionAppeal`.
        """
        appeal = ConstitutionAppeal.create(
            appellant_id=appellant_id,
            contested_entity_id=contested_entity_id,
            contested_entity_type=contested_entity_type,
            grounds=grounds,
            evidence=evidence,
        )

        async with self._lock:
            self.appeals.append(appeal)

        await self._observability.log(
            event_type="constitution_appeal_filed",
            source="ConstitutionEngine",
            agent_id=appellant_id,
            details={
                "appeal_id": appeal.appeal_id,
                "contested_entity_id": contested_entity_id,
                "contested_entity_type": contested_entity_type,
                "grounds": grounds,
                "evidence_count": len(evidence),
            },
        )

        return appeal

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------

    async def review_appeal(self, appeal_id: str) -> ConstitutionRuling:
        """Conduct a full constitutional review of an appeal.

        The review evaluates three axes:

        1. **Procedural correctness** -- whether the original decision
           followed due process requirements for its entity type.
        2. **Evidence sufficiency** -- whether the evidence supporting
           the original decision meets minimum standards.
        3. **Proportionality** -- whether the outcome was proportional
           to the underlying conduct or circumstances.

        Based on these three checks, a verdict is returned:

        - ``UPHELD`` -- the original decision stands.
        - ``OVERTURNED`` -- the original decision is reversed.
        - ``RESCINDED`` -- the original decision is cancelled entirely.
        - ``REMANDED`` -- sent back for a new proceeding.

        Parameters
        ----------
        appeal_id:
            The identifier of the appeal to review.

        Raises
        ------
        ValueError
            If no appeal with the given *appeal_id* exists.
        """
        appeal = await self.get_appeal(appeal_id)
        if appeal is None:
            raise ValueError(f"No appeal found with id '{appeal_id}'.")

        # --- Three-part review ---
        procedural_correct, procedural_detail = _assess_procedural_correctness(
            appeal.contested_entity_type,
            appeal.evidence,
        )

        evidence_sufficient, evidence_detail = _assess_evidence_sufficiency(
            appeal.evidence,
            appeal.contested_entity_type,
        )

        proportional, proportionality_detail = _assess_proportionality(
            appeal.contested_entity_type,
            appeal.grounds,
            appeal.evidence,
        )

        verdict, reasoning = _compute_verdict(
            procedural_correct=procedural_correct,
            evidence_sufficient=evidence_sufficient,
            proportional=proportional,
            procedural_detail=procedural_detail,
            evidence_detail=evidence_detail,
            proportionality_detail=proportionality_detail,
        )

        ruling = ConstitutionRuling.create(
            appeal=appeal,
            verdict=verdict,
            reasoning=reasoning,
        )

        async with self._lock:
            self.rulings.append(ruling)

        await self._observability.log(
            event_type="constitution_ruling",
            source="ConstitutionEngine",
            agent_id=appeal.appellant_id,
            details={
                "appeal_id": appeal_id,
                "verdict": verdict.value,
                "procedural_correct": procedural_correct,
                "evidence_sufficient": evidence_sufficient,
                "proportional": proportional,
                "reasoning": reasoning,
            },
        )

        return ruling

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def get_appeal(self, appeal_id: str) -> ConstitutionAppeal | None:
        """Return the appeal with the given *appeal_id*, or ``None``."""
        async with self._lock:
            for appeal in self.appeals:
                if appeal.appeal_id == appeal_id:
                    return appeal
        return None

    async def get_ruling(self, appeal_id: str) -> ConstitutionRuling | None:
        """Return the ruling for *appeal_id*, or ``None`` if not yet ruled."""
        async with self._lock:
            for ruling in self.rulings:
                if ruling.appeal.appeal_id == appeal_id:
                    return ruling
        return None

    async def get_appeals_by_agent(self, agent_id: str) -> list[ConstitutionAppeal]:
        """Return all appeals filed by *agent_id*."""
        async with self._lock:
            return [
                appeal
                for appeal in self.appeals
                if appeal.appellant_id == agent_id
            ]

    async def get_rulings_by_verdict(
        self,
        verdict: ConstitutionVerdict,
    ) -> list[ConstitutionRuling]:
        """Return all rulings matching the given *verdict*."""
        async with self._lock:
            return [
                ruling
                for ruling in self.rulings
                if ruling.verdict == verdict
            ]
