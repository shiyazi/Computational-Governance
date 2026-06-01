"""Tests for CGC governance layer: elevation, discipline, constitution, reputation, feedback."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.foundation.task_state import TaskStateCore
from cgc.governance.constitution import ConstitutionEngine
from cgc.governance.discipline import DisciplineSystem
from cgc.governance.elevation import ElevationEngine
from cgc.governance.feedback import FeedbackAttributionLayer
from cgc.governance.reputation import ReputationRating
from cgc.models.agent import AgentLevel, AgentProfile, AgentRole, AgentStatus
from cgc.models.governance import (
    ConstitutionVerdict,
    DisciplineAction,
    DisciplineReasonCode,
    FeedbackCategory,
)


# ======================================================================
# Shared fixtures
# ======================================================================

@pytest_asyncio.fixture
async def registry():
    return AgentRegistry()


@pytest_asyncio.fixture
async def task_core():
    return TaskStateCore()


@pytest_asyncio.fixture
async def obs():
    return ObservabilityLog()


@pytest_asyncio.fixture
async def reputation(task_core, obs):
    return ReputationRating(task_core, obs)


@pytest_asyncio.fixture
async def elevation(registry, reputation, obs):
    return ElevationEngine(registry, reputation, obs)


@pytest_asyncio.fixture
async def discipline(registry, obs):
    return DisciplineSystem(registry, obs)


@pytest_asyncio.fixture
async def constitution(registry, obs):
    return ConstitutionEngine(registry, obs)


@pytest_asyncio.fixture
async def feedback(task_core, registry, obs):
    return FeedbackAttributionLayer(task_core, registry, obs)


@pytest_asyncio.fixture
async def good_agent(registry, reputation):
    """Agent with excellent reputation metrics."""
    agent = AgentProfile.create(
        name="GoodAgent", role=AgentRole.EXECUTOR, level=AgentLevel.INTERMEDIATE,
    )
    await registry.register(agent)
    # Build a good reputation profile
    for i in range(20):
        await reputation.record_task_completion(agent.agent_id, f"task_{i}", success=True, first_pass=True)
    await reputation.update_profile(agent.agent_id)
    # Clear discipline events so eligibility check passes
    await reputation.clear_discipline_events(agent.agent_id)
    return agent


@pytest_asyncio.fixture
async def bad_agent(registry):
    """Agent with poor metrics stored in metadata."""
    agent = AgentProfile.create(
        name="BadAgent",
        role=AgentRole.EXECUTOR,
        level=AgentLevel.JUNIOR,
        metadata={
            "reputation": {
                "human_correction_rate": 0.6,
                "rework_rate": 0.7,
                "downstream_breakage": 0.5,
                "risk_tendency": 0.8,
            }
        },
    )
    await registry.register(agent)
    return agent


# ======================================================================
# ElevationEngine tests
# ======================================================================

@pytest.mark.asyncio
class TestElevationEngine:

    async def test_check_eligibility_pass(self, elevation, good_agent, reputation):
        result = await elevation.check_eligibility(good_agent.agent_id)
        assert result["eligible"] is True
        assert result["failed_gates"] == []

    async def test_check_eligibility_fail_no_history(self, elevation, registry):
        """Agent with no task history gets a default profile with all zeros, failing metric gates."""
        agent = AgentProfile.create(
            name="NewAgent", role=AgentRole.EXECUTOR, level=AgentLevel.NOVICE,
        )
        await registry.register(agent)
        result = await elevation.check_eligibility(agent.agent_id)
        assert result["eligible"] is False
        # Default profile has completion_rate=0.0 which fails the 0.8 gate
        assert "completion_rate" in result["failed_gates"]

    async def test_check_eligibility_fail_low_completion(self, elevation, registry, reputation):
        agent = AgentProfile.create(
            name="LowCompletion", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR,
        )
        await registry.register(agent)
        # Mix of success and failure to get completion_rate below 0.8
        for i in range(10):
            await reputation.record_task_completion(agent.agent_id, f"t_{i}", success=False, first_pass=False)
        await reputation.update_profile(agent.agent_id)
        await reputation.clear_discipline_events(agent.agent_id)

        result = await elevation.check_eligibility(agent.agent_id)
        assert result["eligible"] is False
        assert "completion_rate" in result["failed_gates"]

    async def test_request_elevation_success(self, elevation, good_agent):
        request = await elevation.request_elevation(good_agent.agent_id, AgentLevel.SENIOR)
        assert request.agent_id == good_agent.agent_id
        assert request.target_level == AgentLevel.SENIOR
        assert len(elevation.pending_requests) == 1

    async def test_request_elevation_not_eligible(self, elevation, registry):
        agent = AgentProfile.create(
            name="NewAgent", role=AgentRole.EXECUTOR, level=AgentLevel.NOVICE,
        )
        await registry.register(agent)
        with pytest.raises(ValueError, match="not eligible"):
            await elevation.request_elevation(agent.agent_id, AgentLevel.JUNIOR)

    async def test_evaluate_approve(self, elevation, good_agent, reputation):
        # Ensure good profile and no discipline events
        await reputation.update_profile(good_agent.agent_id)
        await reputation.clear_discipline_events(good_agent.agent_id)

        request = await elevation.request_elevation(good_agent.agent_id, AgentLevel.SENIOR)
        decision = await elevation.evaluate(request)
        assert decision.approved is True

    async def test_evaluate_deny_window_closed(self, elevation, good_agent, reputation):
        await reputation.update_profile(good_agent.agent_id)
        await reputation.clear_discipline_events(good_agent.agent_id)

        elevation.config["promotion_window_open"] = False
        request = await elevation.request_elevation(good_agent.agent_id, AgentLevel.SENIOR)
        decision = await elevation.evaluate(request)
        assert decision.approved is False
        assert "promotion_window_closed" in decision.reason

    async def test_evaluate_deny_high_level_ratio(self, elevation, registry, reputation):
        # Register several senior+ agents to push ratio above threshold
        for i in range(5):
            a = AgentProfile.create(
                name=f"Senior_{i}", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR,
            )
            await registry.register(a)
        # Register the candidate as intermediate
        candidate = AgentProfile.create(
            name="Candidate", role=AgentRole.EXECUTOR, level=AgentLevel.INTERMEDIATE,
        )
        await registry.register(candidate)
        for j in range(20):
            await reputation.record_task_completion(candidate.agent_id, f"t_{j}", success=True, first_pass=True)
        await reputation.update_profile(candidate.agent_id)
        await reputation.clear_discipline_events(candidate.agent_id)

        ratio = await elevation.check_high_level_ratio()
        assert ratio >= 0.3  # 5 out of 6 = 0.83

        request = await elevation.request_elevation(candidate.agent_id, AgentLevel.SENIOR)
        decision = await elevation.evaluate(request)
        assert decision.approved is False
        assert "high_level_ratio_exceeded" in decision.reason

    async def test_check_high_level_ratio(self, elevation, registry):
        a1 = AgentProfile.create(name="S1", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR)
        a2 = AgentProfile.create(name="J1", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR)
        a3 = AgentProfile.create(name="J2", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR)
        await registry.register(a1)
        await registry.register(a2)
        await registry.register(a3)
        ratio = await elevation.check_high_level_ratio()
        assert ratio == pytest.approx(1.0 / 3.0, abs=0.01)

    async def test_check_high_level_ratio_empty(self, elevation):
        ratio = await elevation.check_high_level_ratio()
        assert ratio == 0.0

    async def test_tighten_promotion(self, elevation, registry):
        # All agents are senior -> ratio = 1.0 > 0.3
        for i in range(3):
            a = AgentProfile.create(name=f"S_{i}", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR)
            await registry.register(a)

        # First tighten: level 0 -> 1 (raises gates, window stays open)
        await elevation.tighten_promotion()
        assert elevation.config["gate_threshold_level"] == 1
        assert elevation.config["promotion_window_open"] is True
        assert elevation.config["gate_thresholds"]["completion_rate"] > 0.8

        # Second tighten: level 1 -> 2 (window closes)
        await elevation.tighten_promotion()
        assert elevation.config["gate_threshold_level"] == 2
        assert elevation.config["promotion_window_open"] is False

        # Third tighten: level 2 -> 3 (strictest)
        await elevation.tighten_promotion()
        assert elevation.config["gate_threshold_level"] == 3
        assert elevation.config["gate_thresholds"]["completion_rate"] == 0.95

    async def test_demote(self, elevation, registry):
        agent = AgentProfile.create(
            name="ToDemote", role=AgentRole.EXECUTOR, level=AgentLevel.INTERMEDIATE,
        )
        await registry.register(agent)
        decision = await elevation.demote(agent.agent_id, "poor performance")
        assert decision.approved is True
        assert "Demotion" in decision.reason

        updated = await registry.get(agent.agent_id)
        assert updated.level == AgentLevel.JUNIOR

    async def test_demote_does_not_go_below_novice(self, elevation, registry):
        agent = AgentProfile.create(
            name="NoviceAgent", role=AgentRole.EXECUTOR, level=AgentLevel.NOVICE,
        )
        await registry.register(agent)
        decision = await elevation.demote(agent.agent_id, "testing floor")
        assert decision.approved is True

        updated = await registry.get(agent.agent_id)
        assert updated.level == AgentLevel.NOVICE

    async def test_demote_agent_not_found(self, elevation):
        with pytest.raises(ValueError, match="Agent not found"):
            await elevation.demote("nonexistent", "reason")

    async def test_process_pending(self, elevation, good_agent, reputation):
        await reputation.update_profile(good_agent.agent_id)
        await reputation.clear_discipline_events(good_agent.agent_id)

        await elevation.request_elevation(good_agent.agent_id, AgentLevel.SENIOR)
        decisions = await elevation.process_pending()
        assert len(decisions) == 1

    async def test_open_promotion_window(self, elevation):
        elevation.config["promotion_window_open"] = False
        await elevation.open_promotion_window()
        assert elevation.config["promotion_window_open"] is True


# ======================================================================
# DisciplineSystem tests
# ======================================================================

@pytest.mark.asyncio
class TestDisciplineSystem:

    async def test_evaluate_trigger_metric_gate_warn(self, discipline):
        suggestion = await discipline.evaluate_trigger(
            "agent_1", "metric_gate", ["high rework rate"],
        )
        assert suggestion is not None
        assert suggestion.agent_id == "agent_1"
        assert suggestion.action == DisciplineAction.WARN
        assert suggestion.reason_code == DisciplineReasonCode.ABNORMAL_FAILURE_RATE

    async def test_evaluate_trigger_behavior_gate_scope_breach(self, discipline):
        suggestion = await discipline.evaluate_trigger(
            "agent_1", "behavior_gate", ["scope_breach detected at runtime"],
        )
        assert suggestion is not None
        assert suggestion.reason_code == DisciplineReasonCode.SCOPE_BREACH
        assert suggestion.action == DisciplineAction.DEMOTE

    async def test_evaluate_trigger_behavior_gate_forged(self, discipline):
        suggestion = await discipline.evaluate_trigger(
            "agent_1", "behavior_gate", ["forged result detected"],
        )
        assert suggestion is not None
        assert suggestion.reason_code == DisciplineReasonCode.FORGED_RESULT
        assert suggestion.action == DisciplineAction.RETIRE

    async def test_evaluate_trigger_behavior_gate_empty_evidence(self, discipline):
        suggestion = await discipline.evaluate_trigger(
            "agent_1", "behavior_gate", [],
        )
        assert suggestion is None

    async def test_evaluate_trigger_unknown_type(self, discipline):
        suggestion = await discipline.evaluate_trigger(
            "agent_1", "unknown_type", ["evidence"],
        )
        assert suggestion is None

    async def test_check_metric_gates(self, discipline, bad_agent):
        results = await discipline.check_metric_gates(bad_agent.agent_id)
        assert len(results) > 0
        # Bad agent has high rework_rate, human_correction_rate, downstream_breakage, risk_tendency
        reason_codes = {r.reason_code for r in results}
        assert DisciplineReasonCode.ABNORMAL_FAILURE_RATE in reason_codes

    async def test_check_metric_gates_agent_not_found(self, discipline):
        results = await discipline.check_metric_gates("nonexistent")
        assert results == []

    async def test_check_behavior_gates_scope_breach(self, discipline):
        events = [
            {
                "event_type": "scope_breach",
                "timestamp": time.time(),
                "details": {"description": "accessed restricted resource"},
            },
        ]
        results = await discipline.check_behavior_gates("agent_1", events)
        assert len(results) == 1
        assert results[0].reason_code == DisciplineReasonCode.SCOPE_BREACH

    async def test_check_behavior_gates_forged(self, discipline):
        events = [
            {
                "event_type": "forged_receipt",
                "timestamp": time.time(),
                "details": {"description": "faked test result"},
            },
        ]
        results = await discipline.check_behavior_gates("agent_1", events)
        assert len(results) == 1
        assert results[0].reason_code == DisciplineReasonCode.FORGED_RESULT

    async def test_check_behavior_gates_capability_bypass(self, discipline):
        events = [
            {
                "event_type": "capability_bypass",
                "timestamp": time.time(),
                "details": {"description": "bypassed capability check"},
            },
        ]
        results = await discipline.check_behavior_gates("agent_1", events)
        assert len(results) == 1
        assert results[0].reason_code == DisciplineReasonCode.CAPABILITY_ASSEMBLY_BYPASS

    async def test_check_behavior_gates_high_frequency_bypass(self, discipline):
        events = [
            {
                "event_type": "high_frequency_bypass",
                "timestamp": time.time(),
                "details": {"description": "too many bypasses", "frequency": 10},
            },
        ]
        results = await discipline.check_behavior_gates("agent_1", events)
        assert len(results) == 1
        assert results[0].reason_code == DisciplineReasonCode.HIGH_FREQUENCY_BYPASS

    async def test_violation_counting(self, discipline):
        assert await discipline.get_violation_count("agent_1") == 0
        await discipline.evaluate_trigger("agent_1", "metric_gate", ["issue"])
        assert await discipline.get_violation_count("agent_1") == 1
        await discipline.evaluate_trigger("agent_1", "metric_gate", ["issue"])
        assert await discipline.get_violation_count("agent_1") == 2

    async def test_auto_freeze(self, discipline):
        # Default auto_freeze_threshold is 3
        for i in range(2):
            await discipline.evaluate_trigger("agent_1", "metric_gate", ["issue"])
        assert await discipline.should_auto_freeze("agent_1") is False
        await discipline.evaluate_trigger("agent_1", "metric_gate", ["issue"])
        assert await discipline.should_auto_freeze("agent_1") is True

    async def test_record_suggestion(self, discipline):
        from cgc.models.governance import DisciplineSuggestion
        suggestion = DisciplineSuggestion.create(
            agent_id="a1",
            action=DisciplineAction.WARN,
            reason_code=DisciplineReasonCode.REPEATED_VIOLATION,
            severity="medium",
            evidence=["test"],
        )
        await discipline.record_suggestion(suggestion)
        assert len(discipline.suggestions) == 1
        assert await discipline.get_violation_count("a1") == 1

    async def test_get_suggestions(self, discipline):
        # evaluate_trigger does NOT append to self.suggestions; record_suggestion does
        s1 = await discipline.evaluate_trigger("a1", "metric_gate", ["issue"])
        s2 = await discipline.evaluate_trigger("a2", "metric_gate", ["issue"])
        await discipline.record_suggestion(s1)
        await discipline.record_suggestion(s2)
        all_suggestions = await discipline.get_suggestions()
        assert len(all_suggestions) == 2
        a1_suggestions = await discipline.get_suggestions(agent_id="a1")
        assert len(a1_suggestions) == 1


# ======================================================================
# ConstitutionEngine tests
# ======================================================================

@pytest.mark.asyncio
class TestConstitutionEngine:

    async def test_file_appeal(self, constitution):
        appeal = await constitution.file_appeal(
            appellant_id="a1",
            contested_entity_id="disc_123",
            contested_entity_type="discipline",
            grounds="Excessive punishment for minor issue",
            evidence=["trigger: metric_gate", "severity: medium"],
        )
        assert appeal.appeal_id
        assert appeal.appellant_id == "a1"
        assert len(constitution.appeals) == 1

    async def test_review_appeal_upheld(self, constitution):
        # All three axes pass: discipline with trigger, 3+ evidence, high severity
        appeal = await constitution.file_appeal(
            appellant_id="a1",
            contested_entity_id="disc_1",
            contested_entity_type="discipline",
            grounds="Disagree with outcome",
            evidence=[
                "trigger: scope_breach detected",
                "Additional supporting evidence with high severity",
                "Third piece of evidence",
            ],
        )
        ruling = await constitution.review_appeal(appeal.appeal_id)
        assert ruling.verdict == ConstitutionVerdict.UPHELD

    async def test_review_appeal_rescinded(self, constitution):
        # Both procedural=False (no trigger keyword) AND evidence=False (only 1 item for discipline)
        appeal = await constitution.file_appeal(
            appellant_id="a1",
            contested_entity_id="disc_2",
            contested_entity_type="discipline",
            grounds="No evidence at all",
            evidence=["just one item"],
        )
        ruling = await constitution.review_appeal(appeal.appeal_id)
        assert ruling.verdict == ConstitutionVerdict.RESCINDED

    async def test_review_appeal_remanded(self, constitution):
        # Procedural=False but evidence sufficient: elevation_denial without metric or committee/score keywords
        appeal = await constitution.file_appeal(
            appellant_id="a1",
            contested_entity_id="elev_1",
            contested_entity_type="elevation_denial",
            grounds="Missing documentation",
            evidence=["documentation for denial is missing"],
        )
        ruling = await constitution.review_appeal(appeal.appeal_id)
        assert ruling.verdict == ConstitutionVerdict.REMANDED

    async def test_review_appeal_overturned(self, constitution):
        # Procedural ok, evidence ok, but disproportional: discipline with no severity keywords and "excessive" grounds
        appeal = await constitution.file_appeal(
            appellant_id="a1",
            contested_entity_id="disc_3",
            contested_entity_type="discipline",
            grounds="Excessive punishment for minor issue",
            evidence=[
                "trigger: repeated violations found",
                "Second piece of evidence",
                "Third piece of evidence",
            ],
        )
        ruling = await constitution.review_appeal(appeal.appeal_id)
        assert ruling.verdict == ConstitutionVerdict.OVERTURNED

    async def test_review_appeal_not_found(self, constitution):
        with pytest.raises(ValueError, match="No appeal found"):
            await constitution.review_appeal("nonexistent")

    async def test_get_appeal(self, constitution):
        appeal = await constitution.file_appeal(
            "a1", "e1", "discipline", "grounds", ["ev1"],
        )
        found = await constitution.get_appeal(appeal.appeal_id)
        assert found is not None
        assert found.appeal_id == appeal.appeal_id

    async def test_get_appeal_not_found(self, constitution):
        found = await constitution.get_appeal("nonexistent")
        assert found is None

    async def test_get_appeals_by_agent(self, constitution):
        await constitution.file_appeal("a1", "e1", "discipline", "g", ["ev1", "ev2"])
        await constitution.file_appeal("a1", "e2", "discipline", "g", ["ev1", "ev2"])
        await constitution.file_appeal("a2", "e3", "discipline", "g", ["ev1", "ev2"])
        appeals = await constitution.get_appeals_by_agent("a1")
        assert len(appeals) == 2

    async def test_get_rulings_by_verdict(self, constitution):
        # Use same setup as test_review_appeal_upheld to ensure UPHELD verdict
        appeal = await constitution.file_appeal(
            "a1", "e1", "discipline", "g",
            ["trigger: metric issue with high severity", "ev2", "ev3"],
        )
        await constitution.review_appeal(appeal.appeal_id)
        rulings = await constitution.get_rulings_by_verdict(ConstitutionVerdict.UPHELD)
        assert len(rulings) >= 1


# ======================================================================
# ReputationRating tests
# ======================================================================

@pytest.mark.asyncio
class TestReputationRating:

    async def test_get_profile_creates_default(self, reputation):
        profile = await reputation.get_profile("agent_1")
        assert profile.agent_id == "agent_1"
        assert profile.completion_rate == 0.0

    async def test_record_task_completion(self, reputation):
        await reputation.record_task_completion("agent_1", "task_1", success=True, first_pass=True)
        assert len(reputation._task_outcomes["agent_1"]) == 1

    async def test_record_rework(self, reputation):
        await reputation.record_rework("agent_1", "task_1")
        assert len(reputation._rework_events["agent_1"]) == 1

    async def test_record_downstream_breakage(self, reputation):
        await reputation.record_downstream_breakage("agent_1", "task_1", severity=0.5)
        assert len(reputation._breakage_events["agent_1"]) == 1

    async def test_update_profile(self, reputation):
        for i in range(10):
            await reputation.record_task_completion("agent_1", f"t_{i}", success=True, first_pass=True)
        profile = await reputation.update_profile("agent_1")
        assert profile.completion_rate == 1.0
        assert profile.first_pass_acceptance == 1.0
        assert profile.rework_rate == 0.0

    async def test_get_ranking(self, reputation):
        for i in range(10):
            await reputation.record_task_completion("a1", f"t_a_{i}", success=True, first_pass=True)
        for i in range(5):
            await reputation.record_task_completion("a2", f"t_b_{i}", success=False, first_pass=False)
        await reputation.update_profile("a1")
        await reputation.update_profile("a2")
        ranking = await reputation.get_ranking()
        assert len(ranking) == 2
        # a1 should rank higher than a2
        assert ranking[0][0] == "a1"

    async def test_get_at_risk_agents(self, reputation):
        # a2 has failures so long_term_score may be low
        for i in range(10):
            await reputation.record_task_completion("a1", f"t_a_{i}", success=True, first_pass=True)
        for i in range(5):
            await reputation.record_task_completion("a2", f"t_b_{i}", success=False, first_pass=False)
        await reputation.update_profile("a1")
        await reputation.update_profile("a2")
        at_risk = await reputation.get_at_risk_agents(threshold=0.5)
        # a2 should be at risk
        at_risk_ids = [aid for aid, _ in at_risk]
        assert "a2" in at_risk_ids

    async def test_discipline_events(self, reputation):
        count = await reputation.record_discipline_event("a1")
        assert count == 1
        count = await reputation.record_discipline_event("a1")
        assert count == 2
        assert await reputation.get_discipline_event_count("a1") == 2
        await reputation.clear_discipline_events("a1")
        assert await reputation.get_discipline_event_count("a1") == 0


# ======================================================================
# FeedbackAttribution tests
# ======================================================================

@pytest.mark.asyncio
class TestFeedbackAttribution:

    async def test_attribute_feedback_explicit(self, feedback):
        attributions = await feedback.attribute_feedback(
            task_id="task_1",
            category=FeedbackCategory.DESIGN_ERROR,
            description="Bad design",
            responsible_agents=[
                {"agent_id": "a1", "weight": 0.7},
                {"agent_id": "a2", "weight": 0.3},
            ],
        )
        assert len(attributions) == 2
        weights = {a.agent_id: a.responsibility_weight for a in attributions}
        assert weights["a1"] == 0.7
        assert weights["a2"] == 0.3

    async def test_attribute_feedback_auto_owner_only(self, feedback, task_core):
        await task_core.create_task("task_1")
        await task_core.set_owner("task_1", "a1")
        await task_core.add_participant("task_1", "a1")

        attributions = await feedback.attribute_feedback(
            task_id="task_1",
            category=FeedbackCategory.EXECUTION_ERROR,
            description="Mistake",
        )
        assert len(attributions) == 1
        assert attributions[0].agent_id == "a1"
        assert attributions[0].responsibility_weight == 1.0

    async def test_attribute_feedback_auto_owner_and_participants(self, feedback, task_core):
        await task_core.create_task("task_1")
        await task_core.set_owner("task_1", "owner_1")
        await task_core.add_participant("task_1", "owner_1")
        await task_core.add_participant("task_1", "agent_a")
        await task_core.add_participant("task_1", "agent_b")

        attributions = await feedback.attribute_feedback(
            task_id="task_1",
            category=FeedbackCategory.REVIEW_MISS,
            description="Review failed",
        )
        assert len(attributions) == 3
        owner_attr = [a for a in attributions if a.agent_id == "owner_1"][0]
        assert owner_attr.responsibility_weight == 0.4

    async def test_attribute_feedback_auto_no_task(self, feedback):
        attributions = await feedback.attribute_feedback(
            task_id="nonexistent_task",
            category=FeedbackCategory.EXECUTION_ERROR,
            description="Error",
        )
        assert len(attributions) == 0

    async def test_get_attributions(self, feedback):
        await feedback.attribute_feedback(
            "task_1", FeedbackCategory.DESIGN_ERROR, "err",
            responsible_agents=[{"agent_id": "a1", "weight": 0.5}],
        )
        await feedback.attribute_feedback(
            "task_2", FeedbackCategory.EXECUTION_ERROR, "err",
            responsible_agents=[{"agent_id": "a1", "weight": 0.3}],
        )
        all_attrs = await feedback.get_attributions()
        assert len(all_attrs) == 2
        task1_attrs = await feedback.get_attributions(task_id="task_1")
        assert len(task1_attrs) == 1
        a1_attrs = await feedback.get_attributions(agent_id="a1")
        assert len(a1_attrs) == 2

    async def test_get_agent_attribution_score(self, feedback):
        await feedback.attribute_feedback(
            "task_1", FeedbackCategory.DESIGN_ERROR, "err",
            responsible_agents=[
                {"agent_id": "a1", "weight": 0.6},
                {"agent_id": "a2", "weight": 0.4},
            ],
        )
        score_a1 = await feedback.get_agent_attribution_score("a1")
        score_a2 = await feedback.get_agent_attribution_score("a2")
        assert score_a1 == 0.6
        assert score_a2 == 0.4

    async def test_get_agent_attribution_score_no_attributions(self, feedback):
        score = await feedback.get_agent_attribution_score("nonexistent")
        assert score == 0.0
