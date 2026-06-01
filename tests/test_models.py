"""Tests for CGC data models."""

from __future__ import annotations

import time

import pytest

from cgc.models.agent import AgentLevel, AgentProfile, AgentRole, AgentStatus, ContractType
from cgc.models.capability import (
    CapabilityHandle,
    CapabilityRule,
    CapabilityStrength,
    CapabilityTable,
)
from cgc.models.governance import (
    ConstitutionAppeal,
    ConstitutionRuling,
    ConstitutionVerdict,
    DisciplineAction,
    DisciplineReasonCode,
    DisciplineSuggestion,
    ElevationDecision,
    ElevationRequest,
    FeedbackAttribution,
    FeedbackCategory,
    ReputationProfile,
)
from cgc.models.messages import (
    CapabilityRequest,
    GovernanceMessage,
    Message,
    MessageType,
    TaskMessage,
)
from cgc.models.task import TaskStage, TaskState


# ======================================================================
# Enum tests
# ======================================================================

class TestEnums:
    """Verify all enums have the expected values."""

    def test_message_type_values(self):
        expected = {
            "TASK_SUBMIT", "TASK_ASSIGN", "TASK_RESULT", "TASK_DELEGATE",
            "CAPABILITY_REQUEST", "CAPABILITY_REFRESH", "GOVERNANCE_EVENT",
            "DISCIPLINE_TRIGGER", "ELEVATION_TRIGGER", "FEEDBACK_HUMAN",
            "CONSTITUTION_APPEAL", "LOG_EVENT", "SCHEDULER_TICK",
        }
        actual = {e.value for e in MessageType}
        assert actual == expected

    def test_agent_level_values(self):
        assert AgentLevel.NOVICE == 1
        assert AgentLevel.JUNIOR == 2
        assert AgentLevel.INTERMEDIATE == 3
        assert AgentLevel.SENIOR == 4
        assert AgentLevel.PRINCIPAL == 5

    def test_agent_role_values(self):
        expected = {"PLANNER", "EXECUTOR", "REVIEWER", "EXTERNAL_EXECUTOR"}
        actual = {e.value for e in AgentRole}
        assert actual == expected

    def test_agent_status_values(self):
        expected = {"ACTIVE", "FROZEN", "RETIRED", "OBSERVATION"}
        actual = {e.value for e in AgentStatus}
        assert actual == expected

    def test_contract_type_values(self):
        expected = {"CONTROLLED", "EXTERNAL"}
        actual = {e.value for e in ContractType}
        assert actual == expected

    def test_task_stage_values(self):
        expected = {
            "PENDING", "PLANNING", "DISPATCHED", "EXECUTING",
            "REVIEWING", "COMPLETED", "FAILED", "GOVERNANCE_HOLD",
        }
        actual = {e.value for e in TaskStage}
        assert actual == expected

    def test_capability_strength_values(self):
        expected = {
            "READ_ONLY", "PROPOSE", "PREVIEW", "APPLY_SCOPED", "APPLY_FULL",
        }
        actual = {e.value for e in CapabilityStrength}
        assert actual == expected

    def test_discipline_action_values(self):
        expected = {"WARN", "FREEZE", "DEMOTE", "RETIRE"}
        actual = {e.value for e in DisciplineAction}
        assert actual == expected

    def test_discipline_reason_code_values(self):
        expected = {
            "REPEATED_VIOLATION", "FORGED_RESULT", "SCOPE_BREACH",
            "CAPABILITY_ASSEMBLY_BYPASS", "ABNORMAL_FAILURE_RATE",
            "HIGH_FREQUENCY_BYPASS",
        }
        actual = {e.value for e in DisciplineReasonCode}
        assert actual == expected

    def test_constitution_verdict_values(self):
        expected = {"UPHELD", "OVERTURNED", "RESCINDED", "REMANDED"}
        actual = {e.value for e in ConstitutionVerdict}
        assert actual == expected

    def test_feedback_category_values(self):
        expected = {
            "DESIGN_ERROR", "EXECUTION_ERROR", "REVIEW_MISS",
            "DELEGATION_ERROR", "EXTERNAL_DEPENDENCY",
        }
        actual = {e.value for e in FeedbackCategory}
        assert actual == expected


# ======================================================================
# Message tests
# ======================================================================

class TestMessage:
    """Test Message and its subclasses."""

    def test_message_create_with_defaults(self):
        msg = Message.create(
            msg_type=MessageType.TASK_SUBMIT,
            sender="agent_1",
        )
        assert msg.msg_id  # non-empty string
        assert msg.msg_type == MessageType.TASK_SUBMIT
        assert msg.sender == "agent_1"
        assert msg.receiver is None
        assert msg.payload == {}
        assert msg.timestamp > 0
        assert msg.task_id is None
        assert msg.correlation_id is None

    def test_message_create_with_all_fields(self):
        msg = Message.create(
            msg_type=MessageType.TASK_ASSIGN,
            sender="dispatcher",
            receiver="agent_2",
            payload={"key": "value"},
            task_id="task_123",
            correlation_id="corr_456",
        )
        assert msg.receiver == "agent_2"
        assert msg.payload == {"key": "value"}
        assert msg.task_id == "task_123"
        assert msg.correlation_id == "corr_456"

    def test_task_message_create(self):
        msg = TaskMessage.create(
            msg_type=MessageType.TASK_SUBMIT,
            sender="agent_1",
            task_action="submit",
            task_payload={"data": 42},
            priority=5,
        )
        assert isinstance(msg, Message)
        assert msg.task_action == "submit"
        assert msg.task_payload == {"data": 42}
        assert msg.priority == 5
        assert msg.retry_count == 0
        assert msg.max_retries == 3

    def test_governance_message_create(self):
        msg = GovernanceMessage.create(
            msg_type=MessageType.GOVERNANCE_EVENT,
            sender="system",
            governance_type="discipline",
            severity="high",
            authority_id="auth_1",
            affected_agents=["a1", "a2"],
            ruling={"action": "FREEZE"},
        )
        assert isinstance(msg, Message)
        assert msg.governance_type == "discipline"
        assert msg.severity == "high"
        assert msg.authority_id == "auth_1"
        assert msg.affected_agents == ["a1", "a2"]
        assert msg.ruling == {"action": "FREEZE"}

    def test_capability_request_create(self):
        msg = CapabilityRequest.create(
            sender="dispatcher",
            agent_id="agent_x",
            requested_capabilities=["read", "write"],
            context={"task_id": "t1"},
        )
        assert isinstance(msg, Message)
        assert msg.msg_type == MessageType.CAPABILITY_REQUEST
        assert msg.agent_id == "agent_x"
        assert msg.requested_capabilities == ["read", "write"]
        assert msg.context == {"task_id": "t1"}


# ======================================================================
# Agent model tests
# ======================================================================

class TestAgentProfile:
    """Test AgentProfile creation."""

    def test_create_with_defaults(self):
        profile = AgentProfile.create(
            name="Test Agent",
            role=AgentRole.EXECUTOR,
            level=AgentLevel.JUNIOR,
        )
        assert profile.agent_id  # auto-generated
        assert profile.name == "Test Agent"
        assert profile.role == AgentRole.EXECUTOR
        assert profile.level == AgentLevel.JUNIOR
        assert profile.contract_type == ContractType.CONTROLLED
        assert profile.status == AgentStatus.ACTIVE
        assert profile.load == 0
        assert profile.current_task_ids == []
        assert profile.capabilities == []
        assert profile.metadata == {}
        assert profile.registered_at > 0

    def test_create_with_all_fields(self):
        profile = AgentProfile.create(
            name="Senior",
            role=AgentRole.REVIEWER,
            level=AgentLevel.SENIOR,
            contract_type=ContractType.EXTERNAL,
            status=AgentStatus.OBSERVATION,
            load=3,
            current_task_ids=["t1"],
            capabilities=["cap_a"],
            metadata={"region": "us"},
        )
        assert profile.contract_type == ContractType.EXTERNAL
        assert profile.status == AgentStatus.OBSERVATION
        assert profile.load == 3
        assert profile.current_task_ids == ["t1"]
        assert profile.capabilities == ["cap_a"]
        assert profile.metadata == {"region": "us"}


# ======================================================================
# Task model tests
# ======================================================================

class TestTaskState:
    """Test TaskState creation and transitions."""

    def test_create_with_defaults(self):
        ts = TaskState.create()
        assert ts.task_id  # auto-generated
        assert ts.parent_task_id is None
        assert ts.child_task_ids == []
        assert ts.stage == TaskStage.PENDING
        assert ts.round_num == 0
        assert ts.turn == 0
        assert ts.owner is None
        assert ts.participants == []
        assert ts.constraints == {}
        assert ts.artifact_refs == []
        assert ts.in_governance is False
        assert ts.created_at > 0
        assert ts.updated_at == ts.created_at
        assert ts.metadata == {}

    def test_create_with_all_fields(self):
        ts = TaskState.create(
            parent_task_id="parent_1",
            stage=TaskStage.EXECUTING,
            owner="agent_1",
            participants=["agent_1", "agent_2"],
            metadata={"priority": "high"},
        )
        assert ts.parent_task_id == "parent_1"
        assert ts.stage == TaskStage.EXECUTING
        assert ts.owner == "agent_1"
        assert ts.participants == ["agent_1", "agent_2"]
        assert ts.metadata == {"priority": "high"}


# ======================================================================
# Capability model tests
# ======================================================================

class TestCapabilityModels:
    """Test CapabilityHandle, CapabilityTable, CapabilityRule."""

    def test_capability_handle(self):
        handle = CapabilityHandle(
            name="read_files",
            strength=CapabilityStrength.READ_ONLY,
            constraints={"scope": "project"},
            description="Read project files",
        )
        assert handle.name == "read_files"
        assert handle.strength == CapabilityStrength.READ_ONLY
        assert handle.constraints == {"scope": "project"}
        assert handle.description == "Read project files"

    def test_capability_table(self):
        handles = [
            CapabilityHandle("cap_a", CapabilityStrength.PROPOSE, {}, "desc a"),
            CapabilityHandle("cap_b", CapabilityStrength.APPLY_FULL, {}, "desc b"),
        ]
        table = CapabilityTable(
            agent_id="agent_1",
            handles=handles,
            valid_until=time.time() + 3600,
            context_hash="abc123",
        )
        assert table.agent_id == "agent_1"
        assert len(table.handles) == 2
        assert table.valid_until is not None

    def test_capability_rule(self):
        rule = CapabilityRule(
            required_level=AgentLevel.SENIOR,
            required_roles=[AgentRole.EXECUTOR],
            allowed_strengths=[CapabilityStrength.APPLY_SCOPED, CapabilityStrength.APPLY_FULL],
            task_stages=[TaskStage.EXECUTING],
            max_strength=CapabilityStrength.APPLY_FULL,
        )
        assert rule.required_level == AgentLevel.SENIOR
        assert AgentRole.EXECUTOR in rule.required_roles
        assert len(rule.allowed_strengths) == 2


# ======================================================================
# Governance model tests
# ======================================================================

class TestGovernanceModels:
    """Test governance data models."""

    def test_discipline_suggestion_create(self):
        ds = DisciplineSuggestion.create(
            agent_id="a1",
            action=DisciplineAction.WARN,
            reason_code=DisciplineReasonCode.REPEATED_VIOLATION,
            severity="medium",
            evidence=["violation_1"],
        )
        assert ds.agent_id == "a1"
        assert ds.action == DisciplineAction.WARN
        assert ds.reason_code == DisciplineReasonCode.REPEATED_VIOLATION
        assert ds.severity == "medium"
        assert ds.suggested_duration is None
        assert ds.evidence == ["violation_1"]
        assert ds.timestamp > 0

    def test_elevation_request_create(self):
        er = ElevationRequest.create(
            agent_id="a1",
            target_level=AgentLevel.SENIOR,
            metrics={"completion_rate": 0.95},
        )
        assert er.agent_id == "a1"
        assert er.target_level == AgentLevel.SENIOR
        assert er.metrics == {"completion_rate": 0.95}
        assert er.timestamp > 0

    def test_elevation_request_create_defaults(self):
        er = ElevationRequest.create(
            agent_id="a1",
            target_level=AgentLevel.INTERMEDIATE,
        )
        assert er.metrics == {}

    def test_elevation_decision_create(self):
        req = ElevationRequest.create(
            agent_id="a1",
            target_level=AgentLevel.SENIOR,
        )
        dec = ElevationDecision.create(
            request=req,
            approved=True,
            reason="All checks passed.",
            committee_scores={"profile_check": True},
        )
        assert dec.request is req
        assert dec.approved is True
        assert dec.reason == "All checks passed."
        assert dec.committee_scores == {"profile_check": True}
        assert dec.timestamp > 0

    def test_constitution_appeal_create(self):
        ca = ConstitutionAppeal.create(
            appellant_id="a1",
            contested_entity_id="disc_123",
            contested_entity_type="discipline",
            grounds="Excessive punishment",
            evidence=["evidence_a", "evidence_b"],
        )
        assert ca.appeal_id  # auto-generated
        assert ca.appellant_id == "a1"
        assert ca.contested_entity_id == "disc_123"
        assert ca.contested_entity_type == "discipline"
        assert ca.grounds == "Excessive punishment"
        assert ca.evidence == ["evidence_a", "evidence_b"]

    def test_constitution_appeal_create_defaults(self):
        ca = ConstitutionAppeal.create(
            appellant_id="a1",
            contested_entity_id="elev_456",
            contested_entity_type="elevation_denial",
            grounds="No reason given",
        )
        assert ca.evidence == []

    def test_constitution_ruling_create(self):
        appeal = ConstitutionAppeal.create(
            appellant_id="a1",
            contested_entity_id="d1",
            contested_entity_type="discipline",
            grounds="Test",
            evidence=["e1"],
        )
        ruling = ConstitutionRuling.create(
            appeal=appeal,
            verdict=ConstitutionVerdict.UPHELD,
            reasoning="All checks passed.",
        )
        assert ruling.appeal is appeal
        assert ruling.verdict == ConstitutionVerdict.UPHELD
        assert ruling.reasoning == "All checks passed."
        assert ruling.timestamp > 0

    def test_feedback_attribution_create(self):
        fa = FeedbackAttribution.create(
            task_id="t1",
            agent_id="a1",
            category=FeedbackCategory.DESIGN_ERROR,
            responsibility_weight=0.8,
            description="Bad design choice",
        )
        assert fa.feedback_id  # auto-generated
        assert fa.task_id == "t1"
        assert fa.agent_id == "a1"
        assert fa.category == FeedbackCategory.DESIGN_ERROR
        assert fa.responsibility_weight == 0.8
        assert fa.description == "Bad design choice"
        assert fa.timestamp > 0

    def test_reputation_profile_create_defaults(self):
        rp = ReputationProfile.create(agent_id="a1")
        assert rp.agent_id == "a1"
        assert rp.completion_rate == 0.0
        assert rp.first_pass_acceptance == 0.0
        assert rp.rework_rate == 0.0
        assert rp.downstream_breakage == 0.0
        assert rp.human_correction_rate == 0.0
        assert rp.stability == 0.0
        assert rp.role_fitness == 0.0
        assert rp.delegation_quality == 0.0
        assert rp.review_quality == 0.0
        assert rp.risk_tendency == 0.0
        assert rp.long_term_score == 0.0
        assert rp.short_term_score == 0.0
        assert rp.maturity_score == 0.0
        assert rp.last_updated > 0

    def test_reputation_profile_create_with_values(self):
        rp = ReputationProfile.create(
            agent_id="a1",
            completion_rate=0.9,
            first_pass_acceptance=0.85,
            long_term_score=0.8,
        )
        assert rp.completion_rate == 0.9
        assert rp.first_pass_acceptance == 0.85
        assert rp.long_term_score == 0.8
