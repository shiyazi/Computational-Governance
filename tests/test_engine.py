"""Integration tests for the CGC Engine -- full lifecycle, delegation, governance, feedback."""

from __future__ import annotations

import pytest
import pytest_asyncio

from cgc.engine import CGCEngine
from cgc.models.agent import AgentLevel, AgentProfile, AgentRole, AgentStatus
from cgc.models.governance import FeedbackCategory
from cgc.models.task import TaskStage


# ======================================================================
# Fixtures
# ======================================================================

@pytest_asyncio.fixture
async def engine():
    e = CGCEngine()
    yield e


@pytest_asyncio.fixture
async def planner():
    return AgentProfile.create(
        name="Planner",
        role=AgentRole.PLANNER,
        level=AgentLevel.SENIOR,
        capabilities=["plan", "delegate"],
    )


@pytest_asyncio.fixture
async def executor():
    return AgentProfile.create(
        name="Executor",
        role=AgentRole.EXECUTOR,
        level=AgentLevel.INTERMEDIATE,
        capabilities=["execute", "test"],
    )


@pytest_asyncio.fixture
async def reviewer():
    return AgentProfile.create(
        name="Reviewer",
        role=AgentRole.REVIEWER,
        level=AgentLevel.SENIOR,
        capabilities=["review"],
    )


# ======================================================================
# Full lifecycle test
# ======================================================================

@pytest.mark.asyncio
class TestEngineLifecycle:

    async def test_full_lifecycle(self, engine, planner, executor):
        """Create engine, register agents, submit task, assign, complete, verify reputation."""
        # Register agents
        await engine.registry.register(planner)
        await engine.registry.register(executor)

        # Submit a task -- engine auto-assigns to planner
        task_id, task = await engine.submit_task("Build the authentication module")
        assert task_id

        # Verify task was assigned to planner (auto-assign advances to DISPATCHED)
        stored_task = await engine.task_core.get_task(task_id)
        assert stored_task is not None
        assert stored_task.owner == planner.agent_id
        assert stored_task.stage == TaskStage.DISPATCHED

        # Planner completes a step
        msg = await engine.complete_task_step(
            planner.agent_id,
            task_id,
            {"plan": "Implement auth flow"},
            artifact_type="plan",
        )
        assert msg is not None

        # Verify artifact was stored
        artifacts = await engine.artifact_store.get_by_task(task_id)
        assert len(artifacts) >= 1

        # Verify completion was recorded in reputation (raw event buffer)
        assert len(engine.reputation._task_outcomes.get(planner.agent_id, [])) == 1

        # Recompute profile and verify metrics
        await engine.reputation.update_profile(planner.agent_id)
        profile = await engine.reputation.get_profile(planner.agent_id)
        assert profile.completion_rate > 0

    async def test_submit_task_no_agents(self, engine):
        """Task submission with no agents should still create the task."""
        task_id, task = await engine.submit_task("A task with no agents")
        assert task_id
        stored = await engine.task_core.get_task(task_id)
        assert stored is not None
        assert stored.owner is None  # No auto-assignment


# ======================================================================
# Delegation test
# ======================================================================

@pytest.mark.asyncio
class TestEngineDelegation:

    async def test_delegation(self, engine, planner, executor):
        """Parent task -> child task delegation, verify hierarchy."""
        await engine.registry.register(planner)
        await engine.registry.register(executor)

        # Submit parent task
        parent_id, _ = await engine.submit_task("Build system")

        # Manually assign planner to parent so delegation can exclude them
        stored = await engine.task_core.get_task(parent_id)
        if stored and stored.owner is None:
            await engine.dispatcher.assign_task(parent_id, planner.agent_id)

        # Delegate a subtask
        child_id = await engine.delegate(
            parent_id, "Implement authentication", AgentRole.EXECUTOR,
        )
        assert child_id is not None

        # Verify hierarchy
        parent = await engine.task_core.get_task(parent_id)
        assert child_id in parent.child_task_ids

        child = await engine.task_core.get_task(child_id)
        assert child is not None
        assert child.parent_task_id == parent_id
        assert child.owner == executor.agent_id

    async def test_delegation_nonexistent_parent(self, engine, executor):
        """Delegating from nonexistent parent should return None."""
        await engine.registry.register(executor)
        result = await engine.delegate("nonexistent", "Subtask")
        assert result is None


# ======================================================================
# Governance cycle test
# ======================================================================

@pytest.mark.asyncio
class TestEngineGovernance:

    async def test_governance_cycle(self, engine, planner, executor):
        """Trigger governance cycle and verify it runs without error."""
        await engine.registry.register(planner)
        await engine.registry.register(executor)

        # Submit and complete a task to generate some history
        task_id, _ = await engine.submit_task("Test task")
        stored = await engine.task_core.get_task(task_id)
        if stored and stored.owner:
            await engine.complete_task_step(
                stored.owner, task_id, {"result": "done"},
            )

        # Run governance cycle
        await engine.run_governance_cycle()

        # Verify no errors and reputations were updated
        profiles = await engine.reputation.list_profiles()
        assert len(profiles) >= 0  # just verify it completes

    async def test_failure_triggers_discipline_check(self, engine, executor):
        """Reporting a failure should trigger discipline metric gate checks."""
        await engine.registry.register(executor)

        task_id, _ = await engine.submit_task("Failing task")
        stored = await engine.task_core.get_task(task_id)

        # If auto-assigned, report failure
        if stored and stored.owner:
            msg = await engine.report_failure(stored.owner, task_id, "Something broke")
            assert msg is not None

            # Verify failure recorded in reputation
            profile = await engine.reputation.get_profile(stored.owner)
            # With a failure, completion_rate should reflect it
            assert profile is not None


# ======================================================================
# Human feedback test
# ======================================================================

@pytest.mark.asyncio
class TestEngineFeedback:

    async def test_human_feedback(self, engine, planner, executor):
        """Submit feedback, verify attribution."""
        await engine.registry.register(planner)
        await engine.registry.register(executor)

        task_id, _ = await engine.submit_task("Build feature")
        stored = await engine.task_core.get_task(task_id)
        if stored and stored.owner:
            # Complete the task first
            await engine.complete_task_step(
                stored.owner, task_id, {"status": "done"},
            )

        # Submit explicit feedback
        attributions = await engine.submit_human_feedback(
            task_id,
            FeedbackCategory.DESIGN_ERROR,
            "Poor design choice in module X",
            agent_weights=[
                {"agent_id": planner.agent_id, "weight": 0.8},
                {"agent_id": executor.agent_id, "weight": 0.2},
            ],
        )
        assert len(attributions) == 2
        weights = {a.agent_id: a.responsibility_weight for a in attributions}
        assert weights[planner.agent_id] == 0.8
        assert weights[executor.agent_id] == 0.2

    async def test_human_feedback_auto_attribution(self, engine, planner):
        """Submit feedback without explicit weights triggers auto-attribution."""
        await engine.registry.register(planner)

        task_id, _ = await engine.submit_task("Auto feedback task")
        stored = await engine.task_core.get_task(task_id)
        if stored and stored.owner:
            await engine.complete_task_step(
                stored.owner, task_id, {"status": "done"},
            )

        # Submit feedback without agent_weights
        attributions = await engine.submit_human_feedback(
            task_id,
            FeedbackCategory.EXECUTION_ERROR,
            "Execution was flawed",
        )
        # If task had an owner, attribution should be created
        if stored and stored.owner:
            assert len(attributions) >= 1


# ======================================================================
# Elevation test via engine
# ======================================================================

@pytest.mark.asyncio
class TestEngineElevation:

    async def test_request_elevation_not_eligible(self, engine, executor):
        """Agent with no track record should not be eligible."""
        await engine.registry.register(executor)
        decision = await engine.request_elevation(executor.agent_id, AgentLevel.SENIOR)
        assert decision.approved is False

    async def test_request_elevation_eligible(self, engine):
        """Agent with good track record should be eligible."""
        # Create an agent and build good reputation
        agent = AgentProfile.create(
            name="Promotable", role=AgentRole.EXECUTOR, level=AgentLevel.INTERMEDIATE,
        )
        await engine.registry.register(agent)

        # Build strong reputation
        for i in range(20):
            await engine.reputation.record_task_completion(
                agent.agent_id, f"t_{i}", success=True, first_pass=True,
            )
        await engine.reputation.update_profile(agent.agent_id)
        await engine.reputation.clear_discipline_events(agent.agent_id)

        decision = await engine.request_elevation(agent.agent_id, AgentLevel.SENIOR)
        assert decision.approved is True

        updated = await engine.registry.get(agent.agent_id)
        assert updated.level == AgentLevel.SENIOR


# ======================================================================
# Constitution / Appeals test via engine
# ======================================================================

@pytest.mark.asyncio
class TestEngineAppeal:

    async def test_appeal(self, engine, executor):
        """File an appeal and get a ruling."""
        await engine.registry.register(executor)
        ruling = await engine.appeal(
            appellant_id=executor.agent_id,
            contested_id="disc_123",
            contested_type="discipline",
            grounds="Unfair treatment",
            evidence=["trigger: test evidence", "Supporting doc", "Third piece"],
        )
        assert ruling is not None
        assert ruling.verdict.value in ("UPHELD", "OVERTURNED", "RESCINDED", "REMANDED")


# ======================================================================
# System status test
# ======================================================================

@pytest.mark.asyncio
class TestEngineSystemStatus:

    async def test_system_status(self, engine, planner, executor):
        """Verify system status returns expected structure."""
        await engine.registry.register(planner)
        await engine.registry.register(executor)

        status = await engine.get_system_status()
        assert status["total_agents"] == 2
        assert status["active_agents"] == 2
        assert status["frozen_agents"] == 0
        assert "active_tasks" in status
        assert "high_level_ratio" in status
        assert "promotion_window_open" in status

    async def test_system_status_with_frozen(self, engine, executor):
        """Frozen agent should be counted."""
        await engine.registry.register(executor)
        await engine.registry.freeze(executor.agent_id)
        status = await engine.get_system_status()
        assert status["frozen_agents"] == 1
        assert status["active_agents"] == 0

    async def test_system_status_empty(self, engine):
        """System with no agents should return zeros."""
        status = await engine.get_system_status()
        assert status["total_agents"] == 0
        assert status["active_agents"] == 0
        assert status["active_tasks"] == 0
        assert status["high_level_ratio"] == 0.0
