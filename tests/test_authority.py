"""Tests for CGC authority protocol: ProjectionEngine and CapabilityNetwork."""

from __future__ import annotations

import pytest
import pytest_asyncio

from cgc.authority.capability_network import CapabilityNetwork
from cgc.authority.projection import ProjectionEngine
from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.foundation.task_state import TaskStateCore
from cgc.models.agent import AgentLevel, AgentProfile, AgentRole
from cgc.models.capability import CapabilityRule, CapabilityStrength, CapabilityTable
from cgc.models.task import TaskStage


# ======================================================================
# Fixtures
# ======================================================================

@pytest_asyncio.fixture
async def engine():
    return ProjectionEngine()


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
async def network(registry, task_core, obs):
    return CapabilityNetwork(registry, task_core, obs)


@pytest_asyncio.fixture
async def novice_agent():
    return AgentProfile.create(
        name="Novice", role=AgentRole.EXECUTOR, level=AgentLevel.NOVICE,
    )


@pytest_asyncio.fixture
async def junior_agent():
    return AgentProfile.create(
        name="Junior", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR,
    )


@pytest_asyncio.fixture
async def senior_agent():
    return AgentProfile.create(
        name="Senior", role=AgentRole.REVIEWER, level=AgentLevel.SENIOR,
    )


@pytest_asyncio.fixture
async def principal_agent():
    return AgentProfile.create(
        name="Principal", role=AgentRole.PLANNER, level=AgentLevel.PRINCIPAL,
    )


# ======================================================================
# ProjectionEngine tests
# ======================================================================

@pytest.mark.asyncio
class TestProjectionEngine:

    async def test_add_rule(self, engine):
        rule = CapabilityRule(
            required_level=AgentLevel.SENIOR,
            required_roles=[AgentRole.REVIEWER],
            allowed_strengths=[CapabilityStrength.APPLY_FULL],
            task_stages=[TaskStage.REVIEWING],
            max_strength=CapabilityStrength.APPLY_FULL,
        )
        engine.add_rule("code_review", rule)
        assert "code_review" in engine.capability_rules

    async def test_remove_rule(self, engine):
        rule = CapabilityRule(
            required_level=AgentLevel.SENIOR,
            required_roles=[AgentRole.EXECUTOR],
            allowed_strengths=[CapabilityStrength.APPLY_FULL],
            task_stages=[TaskStage.EXECUTING],
            max_strength=CapabilityStrength.APPLY_FULL,
        )
        engine.add_rule("deploy", rule)
        engine.remove_rule("deploy")
        assert "deploy" not in engine.capability_rules

    async def test_remove_rule_silent_on_missing(self, engine):
        engine.remove_rule("nonexistent")  # should not raise

    async def test_project_novice_default(self, engine, novice_agent):
        handles = engine.project(novice_agent, None, ["read_files", "write_files"])
        assert len(handles) == 2
        for h in handles:
            assert h.strength == CapabilityStrength.READ_ONLY

    async def test_project_junior_default(self, engine, junior_agent):
        handles = engine.project(junior_agent, None, ["read_files"])
        assert len(handles) == 1
        assert handles[0].strength == CapabilityStrength.PROPOSE

    async def test_project_senior_default(self, engine, senior_agent):
        handles = engine.project(senior_agent, None, ["read_files"])
        assert len(handles) == 1
        assert handles[0].strength == CapabilityStrength.APPLY_FULL

    async def test_project_rule_blocks_low_level(self, engine, junior_agent):
        rule = CapabilityRule(
            required_level=AgentLevel.SENIOR,
            required_roles=[AgentRole.EXECUTOR],
            allowed_strengths=[CapabilityStrength.APPLY_FULL],
            task_stages=[TaskStage.EXECUTING],
            max_strength=CapabilityStrength.APPLY_FULL,
        )
        engine.add_rule("deploy", rule)
        handles = engine.project(junior_agent, None, ["deploy"])
        assert len(handles) == 0

    async def test_project_rule_allows_high_level(self, engine, senior_agent):
        rule = CapabilityRule(
            required_level=AgentLevel.SENIOR,
            required_roles=[AgentRole.REVIEWER],
            allowed_strengths=[CapabilityStrength.APPLY_FULL],
            task_stages=[TaskStage.REVIEWING],
            max_strength=CapabilityStrength.APPLY_FULL,
        )
        engine.add_rule("code_review", rule)
        task = await TaskStateCore().create_task("t1")
        # advance to REVIEWING
        tcore = TaskStateCore()
        task = await tcore.create_task("t1")
        await tcore.update_stage("t1", TaskStage.REVIEWING)
        task_state = await tcore.get_task("t1")

        handles = engine.project(senior_agent, task_state, ["code_review"])
        assert len(handles) == 1
        assert handles[0].name == "code_review"

    async def test_project_rule_blocks_wrong_role(self, engine):
        executor = AgentProfile.create(
            name="Exec", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR,
        )
        rule = CapabilityRule(
            required_level=AgentLevel.SENIOR,
            required_roles=[AgentRole.REVIEWER],
            allowed_strengths=[CapabilityStrength.APPLY_FULL],
            task_stages=[TaskStage.REVIEWING],
            max_strength=CapabilityStrength.APPLY_FULL,
        )
        engine.add_rule("code_review", rule)
        handles = engine.project(executor, None, ["code_review"])
        assert len(handles) == 0

    async def test_project_rule_blocks_wrong_stage(self, engine, senior_agent):
        rule = CapabilityRule(
            required_level=AgentLevel.SENIOR,
            required_roles=[AgentRole.REVIEWER],
            allowed_strengths=[CapabilityStrength.APPLY_FULL],
            task_stages=[TaskStage.REVIEWING],
            max_strength=CapabilityStrength.APPLY_FULL,
        )
        engine.add_rule("code_review", rule)
        tcore = TaskStateCore()
        await tcore.create_task("t1")
        await tcore.update_stage("t1", TaskStage.EXECUTING)
        task_state = await tcore.get_task("t1")

        handles = engine.project(senior_agent, task_state, ["code_review"])
        assert len(handles) == 0

    async def test_project_no_task_context_skips_stage_check(self, engine, senior_agent):
        rule = CapabilityRule(
            required_level=AgentLevel.SENIOR,
            required_roles=[AgentRole.REVIEWER],
            allowed_strengths=[CapabilityStrength.APPLY_FULL],
            task_stages=[TaskStage.REVIEWING],
            max_strength=CapabilityStrength.APPLY_FULL,
        )
        engine.add_rule("code_review", rule)
        # No task state provided -- stage check is skipped
        handles = engine.project(senior_agent, None, ["code_review"])
        assert len(handles) == 1

    async def test_project_mixed_rule_and_default(self, engine, senior_agent):
        rule = CapabilityRule(
            required_level=AgentLevel.SENIOR,
            required_roles=[AgentRole.REVIEWER],
            allowed_strengths=[CapabilityStrength.APPLY_FULL],
            task_stages=[TaskStage.REVIEWING],
            max_strength=CapabilityStrength.APPLY_FULL,
        )
        engine.add_rule("code_review", rule)
        handles = engine.project(senior_agent, None, ["code_review", "generic_cap"])
        assert len(handles) == 2
        names = {h.name for h in handles}
        assert names == {"code_review", "generic_cap"}

    async def test_compute_context_hash(self):
        h1 = ProjectionEngine.compute_context_hash("a1", "t1", 2, "EXECUTOR")
        h2 = ProjectionEngine.compute_context_hash("a1", "t1", 2, "EXECUTOR")
        h3 = ProjectionEngine.compute_context_hash("a2", "t1", 2, "EXECUTOR")
        assert h1 == h2
        assert h1 != h3


# ======================================================================
# CapabilityNetwork tests
# ======================================================================

@pytest.mark.asyncio
class TestCapabilityNetwork:

    async def test_register_capabilities(self, network):
        await network.register_capabilities(["cap_a", "cap_b"])
        cap_set = await network.get_full_capability_set()
        assert "cap_a" in cap_set
        assert "cap_b" in cap_set

    async def test_register_capabilities_dedupes(self, network):
        await network.register_capabilities(["cap_a", "cap_a"])
        cap_set = await network.get_full_capability_set()
        assert cap_set.count("cap_a") == 1

    async def test_project_for_agent(self, network, registry):
        agent = AgentProfile.create(
            name="TestAgent", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR,
        )
        await registry.register(agent)
        await network.register_capabilities(["read", "write"])

        table = await network.project_for_agent(agent.agent_id)
        assert isinstance(table, CapabilityTable)
        assert table.agent_id == agent.agent_id
        assert len(table.handles) == 2
        assert table.context_hash  # non-empty

    async def test_project_for_agent_with_task(self, network, registry, task_core):
        agent = AgentProfile.create(
            name="TestAgent", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR,
        )
        await registry.register(agent)
        await task_core.create_task("task_1")
        await network.register_capabilities(["cap_a"])

        table = await network.project_for_agent(agent.agent_id, task_id="task_1")
        assert isinstance(table, CapabilityTable)

    async def test_project_for_agent_not_found(self, network):
        with pytest.raises(ValueError, match="Agent not found"):
            await network.project_for_agent("nonexistent")

    async def test_refresh_all(self, network, registry):
        a1 = AgentProfile.create(name="A1", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR)
        a2 = AgentProfile.create(name="A2", role=AgentRole.PLANNER, level=AgentLevel.SENIOR)
        await registry.register(a1)
        await registry.register(a2)
        await network.register_capabilities(["cap_x"])

        results = await network.refresh_all()
        assert len(results) == 2
        assert a1.agent_id in results
        assert a2.agent_id in results

    async def test_handle_capability_request(self, network, registry):
        agent = AgentProfile.create(
            name="Agent", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR,
        )
        await registry.register(agent)
        await network.register_capabilities(["cap_a", "cap_b"])

        # Project first so there's a cached table
        await network.project_for_agent(agent.agent_id)

        result = await network.handle_capability_request(
            agent.agent_id, ["cap_a", "cap_b", "cap_c"], {},
        )
        assert result["cap_a"] is True
        assert result["cap_b"] is True
        assert result["cap_c"] is False

    async def test_handle_capability_request_auto_projects(self, network, registry):
        agent = AgentProfile.create(
            name="Agent", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR,
        )
        await registry.register(agent)
        await network.register_capabilities(["cap_a"])

        # No prior projection -- should auto-compute
        result = await network.handle_capability_request(
            agent.agent_id, ["cap_a"], {},
        )
        assert result["cap_a"] is True

    async def test_on_agent_level_change(self, network, registry):
        agent = AgentProfile.create(
            name="Agent", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR,
        )
        await registry.register(agent)
        await network.register_capabilities(["cap_a"])
        await network.project_for_agent(agent.agent_id)

        # Simulate level change
        await registry.update_level(agent.agent_id, AgentLevel.SENIOR)
        await network.on_agent_level_change(agent.agent_id, AgentLevel.SENIOR)

        table = await network.get_table(agent.agent_id)
        assert table is not None
