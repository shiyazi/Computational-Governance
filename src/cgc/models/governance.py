from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from cgc.models.agent import AgentLevel


class DisciplineAction(str, Enum):
    WARN = "WARN"
    FREEZE = "FREEZE"
    DEMOTE = "DEMOTE"
    RETIRE = "RETIRE"


class DisciplineReasonCode(str, Enum):
    REPEATED_VIOLATION = "REPEATED_VIOLATION"
    FORGED_RESULT = "FORGED_RESULT"
    SCOPE_BREACH = "SCOPE_BREACH"
    CAPABILITY_ASSEMBLY_BYPASS = "CAPABILITY_ASSEMBLY_BYPASS"
    ABNORMAL_FAILURE_RATE = "ABNORMAL_FAILURE_RATE"
    HIGH_FREQUENCY_BYPASS = "HIGH_FREQUENCY_BYPASS"


class ConstitutionVerdict(str, Enum):
    UPHELD = "UPHELD"
    OVERTURNED = "OVERTURNED"
    RESCINDED = "RESCINDED"
    REMANDED = "REMANDED"


class FeedbackCategory(str, Enum):
    DESIGN_ERROR = "DESIGN_ERROR"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    REVIEW_MISS = "REVIEW_MISS"
    DELEGATION_ERROR = "DELEGATION_ERROR"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"


@dataclass
class DisciplineSuggestion:
    agent_id: str
    action: DisciplineAction
    reason_code: DisciplineReasonCode
    severity: str
    suggested_duration: str | None
    evidence: list[str]
    timestamp: float

    @classmethod
    def create(
        cls,
        agent_id: str,
        action: DisciplineAction,
        reason_code: DisciplineReasonCode,
        severity: str,
        suggested_duration: str | None = None,
        evidence: list[str] | None = None,
    ) -> DisciplineSuggestion:
        return cls(
            agent_id=agent_id,
            action=action,
            reason_code=reason_code,
            severity=severity,
            suggested_duration=suggested_duration,
            evidence=evidence or [],
            timestamp=time.time(),
        )


@dataclass
class ElevationRequest:
    agent_id: str
    target_level: AgentLevel
    metrics: dict[str, Any]
    timestamp: float

    @classmethod
    def create(
        cls,
        agent_id: str,
        target_level: AgentLevel,
        metrics: dict[str, Any] | None = None,
    ) -> ElevationRequest:
        return cls(
            agent_id=agent_id,
            target_level=target_level,
            metrics=metrics or {},
            timestamp=time.time(),
        )


@dataclass
class ElevationDecision:
    request: ElevationRequest
    approved: bool
    committee_scores: dict[str, Any] | None
    reason: str
    timestamp: float

    @classmethod
    def create(
        cls,
        request: ElevationRequest,
        approved: bool,
        reason: str,
        committee_scores: dict[str, Any] | None = None,
    ) -> ElevationDecision:
        return cls(
            request=request,
            approved=approved,
            committee_scores=committee_scores,
            reason=reason,
            timestamp=time.time(),
        )


@dataclass
class ConstitutionAppeal:
    appeal_id: str
    appellant_id: str
    contested_entity_id: str
    contested_entity_type: str
    grounds: str
    evidence: list[str]
    timestamp: float

    @classmethod
    def create(
        cls,
        appellant_id: str,
        contested_entity_id: str,
        contested_entity_type: str,
        grounds: str,
        evidence: list[str] | None = None,
    ) -> ConstitutionAppeal:
        return cls(
            appeal_id=uuid.uuid4().hex,
            appellant_id=appellant_id,
            contested_entity_id=contested_entity_id,
            contested_entity_type=contested_entity_type,
            grounds=grounds,
            evidence=evidence or [],
            timestamp=time.time(),
        )


@dataclass
class ConstitutionRuling:
    appeal: ConstitutionAppeal
    verdict: ConstitutionVerdict
    reasoning: str
    timestamp: float

    @classmethod
    def create(
        cls,
        appeal: ConstitutionAppeal,
        verdict: ConstitutionVerdict,
        reasoning: str,
    ) -> ConstitutionRuling:
        return cls(
            appeal=appeal,
            verdict=verdict,
            reasoning=reasoning,
            timestamp=time.time(),
        )


@dataclass
class FeedbackAttribution:
    feedback_id: str
    task_id: str
    agent_id: str
    category: FeedbackCategory
    responsibility_weight: float
    description: str
    timestamp: float

    @classmethod
    def create(
        cls,
        task_id: str,
        agent_id: str,
        category: FeedbackCategory,
        responsibility_weight: float,
        description: str,
    ) -> FeedbackAttribution:
        return cls(
            feedback_id=uuid.uuid4().hex,
            task_id=task_id,
            agent_id=agent_id,
            category=category,
            responsibility_weight=responsibility_weight,
            description=description,
            timestamp=time.time(),
        )


@dataclass
class ReputationProfile:
    agent_id: str
    completion_rate: float
    first_pass_acceptance: float
    rework_rate: float
    downstream_breakage: float
    human_correction_rate: float
    stability: float
    role_fitness: float
    delegation_quality: float
    review_quality: float
    risk_tendency: float
    long_term_score: float
    short_term_score: float
    maturity_score: float
    last_updated: float

    @classmethod
    def create(
        cls,
        agent_id: str,
        completion_rate: float = 0.0,
        first_pass_acceptance: float = 0.0,
        rework_rate: float = 0.0,
        downstream_breakage: float = 0.0,
        human_correction_rate: float = 0.0,
        stability: float = 0.0,
        role_fitness: float = 0.0,
        delegation_quality: float = 0.0,
        review_quality: float = 0.0,
        risk_tendency: float = 0.0,
        long_term_score: float = 0.0,
        short_term_score: float = 0.0,
        maturity_score: float = 0.0,
    ) -> ReputationProfile:
        return cls(
            agent_id=agent_id,
            completion_rate=completion_rate,
            first_pass_acceptance=first_pass_acceptance,
            rework_rate=rework_rate,
            downstream_breakage=downstream_breakage,
            human_correction_rate=human_correction_rate,
            stability=stability,
            role_fitness=role_fitness,
            delegation_quality=delegation_quality,
            review_quality=review_quality,
            risk_tendency=risk_tendency,
            long_term_score=long_term_score,
            short_term_score=short_term_score,
            maturity_score=maturity_score,
            last_updated=time.time(),
        )
