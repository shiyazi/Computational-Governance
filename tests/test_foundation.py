"""Tests for CGC foundation modules: TaskStateCore, AgentRegistry, ArtifactStore, ObservabilityLog."""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from cgc.foundation.artifact import ArtifactStore
from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.foundation.task_state import TaskStateCore
from cgc.models.agent import AgentLevel, AgentProfile, AgentRole, AgentStatus, ContractType
from cgc.models.task import TaskStage


# ======================================================================
# Fixtures
# ======================================================================

@pytest_asyncio.fixture
async def task_core():
    return TaskStateCore()


@pytest_asyncio.fixture
async def registry():
    return AgentRegistry()


@pytest_asyncio.fixture
async def artifact_store():
    return ArtifactStore()


@pytest_asyncio.fixture
async def obs_log():
    return ObservabilityLog()


@pytest_asyncio.fixture
async def agent_profile():
    return AgentProfile.create(
        name="TestAgent",
        role=AgentRole.EXECUTOR,
        level=AgentLevel.JUNIOR,
    )


@pytest_asyncio.fixture
async def senior_profile():
    return AgentProfile.create(
        name="SeniorAgent",
        role=AgentRole.REVIEWER,
        level=AgentLevel.SENIOR,
    )


# ======================================================================
# TaskStateCore tests
# ======================================================================

@pytest.mark.asyncio
class TestTaskStateCore:

    async def test_create_task(self, task_core):
        task = await task_core.create_task("task_1")
        assert task.task_id == "task_1"
        assert task.stage == TaskStage.PENDING
        assert task.owner is None
        assert task.participants == []
        assert task.child_task_ids == []
        assert task.round_num == 0
        assert task.turn == 0
        assert task.in_governance is False

    async def test_create_task_with_parent(self, task_core):
        parent = await task_core.create_task("parent_1")
        child = await task_core.create_task("child_1", parent_task_id="parent_1")
        assert child.parent_task_id == "parent_1"

    async def test_create_task_with_metadata(self, task_core):
        task = await task_core.create_task("task_2", metadata={"priority": "high"})
        assert task.metadata == {"priority": "high"}

    async def test_get_task(self, task_core):
        await task_core.create_task("task_1")
        task = await task_core.get_task("task_1")
        assert task is not None
        assert task.task_id == "task_1"

    async def test_get_task_not_found(self, task_core):
        task = await task_core.get_task("nonexistent")
        assert task is None

    async def test_delete_task(self, task_core):
        await task_core.create_task("task_1")
        result = await task_core.delete_task("task_1")
        assert result is True
        assert await task_core.get_task("task_1") is None

    async def test_delete_task_not_found(self, task_core):
        result = await task_core.delete_task("nonexistent")
        assert result is False

    async def test_update_stage(self, task_core):
        await task_core.create_task("task_1")
        task = await task_core.update_stage("task_1", TaskStage.DISPATCHED)
        assert task.stage == TaskStage.DISPATCHED

    async def test_update_stage_not_found(self, task_core):
        with pytest.raises(KeyError):
            await task_core.update_stage("nonexistent", TaskStage.DISPATCHED)

    async def test_advance_turn(self, task_core):
        await task_core.create_task("task_1")
        task = await task_core.advance_turn("task_1")
        assert task.turn == 1
        task = await task_core.advance_turn("task_1")
        assert task.turn == 2

    async def test_advance_round(self, task_core):
        await task_core.create_task("task_1")
        task = await task_core.advance_round("task_1")
        assert task.round_num == 1

    async def test_set_owner(self, task_core):
        await task_core.create_task("task_1")
        task = await task_core.set_owner("task_1", "agent_1")
        assert task.owner == "agent_1"

    async def test_add_participant(self, task_core):
        await task_core.create_task("task_1")
        task = await task_core.add_participant("task_1", "agent_1")
        assert "agent_1" in task.participants
        # Adding same participant again should be idempotent
        task = await task_core.add_participant("task_1", "agent_1")
        assert task.participants.count("agent_1") == 1

    async def test_add_child_task(self, task_core):
        await task_core.create_task("parent_1")
        task = await task_core.add_child_task("parent_1", "child_1")
        assert "child_1" in task.child_task_ids
        # Adding same child again is idempotent
        task = await task_core.add_child_task("parent_1", "child_1")
        assert task.child_task_ids.count("child_1") == 1

    async def test_add_artifact_ref(self, task_core):
        await task_core.create_task("task_1")
        task = await task_core.add_artifact_ref("task_1", "artifact_1")
        assert "artifact_1" in task.artifact_refs

    async def test_set_governance_hold(self, task_core):
        await task_core.create_task("task_1")
        task = await task_core.set_governance_hold("task_1", True)
        assert task.in_governance is True
        task = await task_core.set_governance_hold("task_1", False)
        assert task.in_governance is False

    async def test_get_active_tasks(self, task_core):
        await task_core.create_task("task_1")
        await task_core.create_task("task_2")
        await task_core.create_task("task_3")
        # Complete one task
        await task_core.update_stage("task_3", TaskStage.COMPLETED)
        active = await task_core.get_active_tasks()
        active_ids = {t.task_id for t in active}
        assert "task_1" in active_ids
        assert "task_2" in active_ids
        assert "task_3" not in active_ids

    async def test_get_active_tasks_excludes_failed(self, task_core):
        await task_core.create_task("task_1")
        await task_core.update_stage("task_1", TaskStage.FAILED)
        active = await task_core.get_active_tasks()
        assert len(active) == 0

    async def test_get_tasks_by_owner(self, task_core):
        await task_core.create_task("task_1")
        await task_core.create_task("task_2")
        await task_core.set_owner("task_1", "agent_a")
        await task_core.set_owner("task_2", "agent_b")
        tasks = await task_core.get_tasks_by_owner("agent_a")
        assert len(tasks) == 1
        assert tasks[0].task_id == "task_1"

    async def test_get_child_tasks(self, task_core):
        await task_core.create_task("parent_1")
        await task_core.create_task("child_1", parent_task_id="parent_1")
        await task_core.create_task("child_2", parent_task_id="parent_1")
        children = await task_core.get_child_tasks("parent_1")
        assert len(children) == 2
        child_ids = {c.task_id for c in children}
        assert child_ids == {"child_1", "child_2"}


# ======================================================================
# AgentRegistry tests
# ======================================================================

@pytest.mark.asyncio
class TestAgentRegistry:

    async def test_register(self, registry, agent_profile):
        result = await registry.register(agent_profile)
        assert result is agent_profile

    async def test_register_duplicate_raises(self, registry, agent_profile):
        await registry.register(agent_profile)
        with pytest.raises(ValueError, match="already registered"):
            await registry.register(agent_profile)

    async def test_get(self, registry, agent_profile):
        await registry.register(agent_profile)
        result = await registry.get(agent_profile.agent_id)
        assert result is agent_profile

    async def test_get_not_found(self, registry):
        result = await registry.get("nonexistent")
        assert result is None

    async def test_update_status(self, registry, agent_profile):
        await registry.register(agent_profile)
        updated = await registry.update_status(agent_profile.agent_id, AgentStatus.FROZEN)
        assert updated.status == AgentStatus.FROZEN

    async def test_update_level(self, registry, agent_profile):
        await registry.register(agent_profile)
        updated = await registry.update_level(agent_profile.agent_id, AgentLevel.SENIOR)
        assert updated.level == AgentLevel.SENIOR

    async def test_update_load(self, registry, agent_profile):
        await registry.register(agent_profile)
        updated = await registry.update_load(agent_profile.agent_id, delta=3)
        assert updated.load == 3
        updated = await registry.update_load(agent_profile.agent_id, delta=-1)
        assert updated.load == 2

    async def test_find_available_no_filters(self, registry, agent_profile):
        await registry.register(agent_profile)
        results = await registry.find_available()
        assert len(results) == 1

    async def test_find_available_by_role(self, registry):
        executor = AgentProfile.create(name="E", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR)
        planner = AgentProfile.create(name="P", role=AgentRole.PLANNER, level=AgentLevel.SENIOR)
        await registry.register(executor)
        await registry.register(planner)
        results = await registry.find_available(role=AgentRole.EXECUTOR)
        assert len(results) == 1
        assert results[0].role == AgentRole.EXECUTOR

    async def test_find_available_by_min_level(self, registry):
        junior = AgentProfile.create(name="J", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR)
        senior = AgentProfile.create(name="S", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR)
        await registry.register(junior)
        await registry.register(senior)
        results = await registry.find_available(min_level=AgentLevel.SENIOR)
        assert len(results) == 1
        assert results[0].level == AgentLevel.SENIOR

    async def test_find_available_by_max_load(self, registry):
        busy = AgentProfile.create(name="B", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR, load=5)
        free = AgentProfile.create(name="F", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR, load=1)
        await registry.register(busy)
        await registry.register(free)
        results = await registry.find_available(max_load=2)
        assert len(results) == 1
        assert results[0].name == "F"

    async def test_find_available_excludes_non_active(self, registry):
        frozen = AgentProfile.create(
            name="F", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR,
            status=AgentStatus.FROZEN,
        )
        await registry.register(frozen)
        results = await registry.find_available()
        assert len(results) == 0

    async def test_retire(self, registry, agent_profile):
        await registry.register(agent_profile)
        updated = await registry.retire(agent_profile.agent_id)
        assert updated.status == AgentStatus.RETIRED

    async def test_freeze_unfreeze(self, registry, agent_profile):
        await registry.register(agent_profile)
        updated = await registry.freeze(agent_profile.agent_id)
        assert updated.status == AgentStatus.FROZEN
        updated = await registry.unfreeze(agent_profile.agent_id)
        assert updated.status == AgentStatus.ACTIVE

    async def test_count_by_level(self, registry):
        j1 = AgentProfile.create(name="J1", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR)
        j2 = AgentProfile.create(name="J2", role=AgentRole.PLANNER, level=AgentLevel.JUNIOR)
        s1 = AgentProfile.create(name="S1", role=AgentRole.REVIEWER, level=AgentLevel.SENIOR)
        await registry.register(j1)
        await registry.register(j2)
        await registry.register(s1)
        counts = await registry.count_by_level()
        assert counts[AgentLevel.JUNIOR] == 2
        assert counts[AgentLevel.SENIOR] == 1

    async def test_list_all(self, registry, agent_profile, senior_profile):
        await registry.register(agent_profile)
        await registry.register(senior_profile)
        all_agents = await registry.list_all()
        assert len(all_agents) == 2

    async def test_add_and_remove_task(self, registry, agent_profile):
        await registry.register(agent_profile)
        updated = await registry.add_task(agent_profile.agent_id, "task_1")
        assert "task_1" in updated.current_task_ids
        updated = await registry.remove_task(agent_profile.agent_id, "task_1")
        assert "task_1" not in updated.current_task_ids

    async def test_update_capabilities(self, registry, agent_profile):
        await registry.register(agent_profile)
        updated = await registry.update_capabilities(agent_profile.agent_id, ["cap_a", "cap_b"])
        assert updated.capabilities == ["cap_a", "cap_b"]


# ======================================================================
# ArtifactStore tests
# ======================================================================

@pytest.mark.asyncio
class TestArtifactStore:

    async def test_store_with_explicit_id(self, artifact_store):
        aid = await artifact_store.store("art_1", "plan", {"text": "hello"})
        assert aid == "art_1"

    async def test_store_with_auto_id(self, artifact_store):
        aid = await artifact_store.store(None, "plan", {"text": "hello"})
        assert aid  # non-empty string

    async def test_store_with_all_fields(self, artifact_store):
        aid = await artifact_store.store(
            "art_2", "review", {"rating": 5},
            metadata={"version": 2},
            producer="agent_1",
            task_id="task_1",
        )
        record = await artifact_store.get("art_2")
        assert record["type"] == "review"
        assert record["content"] == {"rating": 5}
        assert record["metadata"] == {"version": 2}
        assert record["producer"] == "agent_1"
        assert record["task_id"] == "task_1"
        assert record["created_at"] > 0

    async def test_get(self, artifact_store):
        await artifact_store.store("art_1", "plan", "content")
        record = await artifact_store.get("art_1")
        assert record is not None
        assert record["id"] == "art_1"

    async def test_get_not_found(self, artifact_store):
        record = await artifact_store.get("nonexistent")
        assert record is None

    async def test_get_by_task(self, artifact_store):
        await artifact_store.store("a1", "plan", "c1", task_id="task_1")
        await artifact_store.store("a2", "review", "c2", task_id="task_1")
        await artifact_store.store("a3", "plan", "c3", task_id="task_2")
        results = await artifact_store.get_by_task("task_1")
        assert len(results) == 2

    async def test_get_by_producer(self, artifact_store):
        await artifact_store.store("a1", "plan", "c1", producer="agent_1")
        await artifact_store.store("a2", "review", "c2", producer="agent_2")
        results = await artifact_store.get_by_producer("agent_1")
        assert len(results) == 1
        assert results[0]["producer"] == "agent_1"

    async def test_get_by_type(self, artifact_store):
        await artifact_store.store("a1", "plan", "c1")
        await artifact_store.store("a2", "review", "c2")
        await artifact_store.store("a3", "plan", "c3")
        results = await artifact_store.get_by_type("plan")
        assert len(results) == 2

    async def test_update_content(self, artifact_store):
        await artifact_store.store("art_1", "plan", "old content")
        updated = await artifact_store.update("art_1", content="new content")
        assert updated["content"] == "new content"

    async def test_update_metadata(self, artifact_store):
        await artifact_store.store("art_1", "plan", "c")
        updated = await artifact_store.update("art_1", metadata={"version": 2})
        assert updated["metadata"] == {"version": 2}

    async def test_update_not_found(self, artifact_store):
        result = await artifact_store.update("nonexistent", content="x")
        assert result is None

    async def test_delete(self, artifact_store):
        await artifact_store.store("art_1", "plan", "c")
        assert await artifact_store.delete("art_1") is True
        assert await artifact_store.get("art_1") is None

    async def test_delete_not_found(self, artifact_store):
        assert await artifact_store.delete("nonexistent") is False

    async def test_list_all(self, artifact_store):
        await artifact_store.store("a1", "plan", "c1")
        await artifact_store.store("a2", "review", "c2")
        all_artifacts = await artifact_store.list_all()
        assert len(all_artifacts) == 2


# ======================================================================
# ObservabilityLog tests
# ======================================================================

@pytest.mark.asyncio
class TestObservabilityLog:

    async def test_log_returns_id(self, obs_log):
        log_id = await obs_log.log("test_event", "test_source")
        assert log_id  # non-empty string

    async def test_log_with_all_fields(self, obs_log):
        log_id = await obs_log.log(
            "dispatch_decision",
            "dispatcher",
            details={"action": "submit"},
            agent_id="agent_1",
            task_id="task_1",
        )
        results = await obs_log.query(event_type="dispatch_decision")
        assert len(results) >= 1
        entry = results[0]
        assert entry["event_type"] == "dispatch_decision"
        assert entry["source"] == "dispatcher"
        assert entry["details"] == {"action": "submit"}
        assert entry["agent_id"] == "agent_1"
        assert entry["task_id"] == "task_1"
        assert entry["timestamp"] > 0

    async def test_query_by_event_type(self, obs_log):
        await obs_log.log("type_a", "src")
        await obs_log.log("type_b", "src")
        await obs_log.log("type_a", "src")
        results = await obs_log.query(event_type="type_a")
        assert len(results) == 2

    async def test_query_by_agent_id(self, obs_log):
        await obs_log.log("event", "src", agent_id="a1")
        await obs_log.log("event", "src", agent_id="a2")
        results = await obs_log.query(agent_id="a1")
        assert len(results) == 1

    async def test_query_by_task_id(self, obs_log):
        await obs_log.log("event", "src", task_id="t1")
        await obs_log.log("event", "src", task_id="t2")
        results = await obs_log.query(task_id="t1")
        assert len(results) == 1

    async def test_query_by_source(self, obs_log):
        await obs_log.log("event", "source_a")
        await obs_log.log("event", "source_b")
        results = await obs_log.query(source="source_a")
        assert len(results) == 1

    async def test_query_by_time_range(self, obs_log):
        before = time.time()
        await asyncio.sleep(0.01)
        await obs_log.log("event", "src")
        await asyncio.sleep(0.01)
        after = time.time()
        # Events between before and after
        results = await obs_log.query(since=before, until=after)
        assert len(results) >= 1
        # No events after the upper bound
        results = await obs_log.query(since=after + 100)
        assert len(results) == 0

    async def test_query_limit(self, obs_log):
        for i in range(10):
            await obs_log.log("event", "src")
        results = await obs_log.query(event_type="event", limit=3)
        assert len(results) == 3

    async def test_query_returns_most_recent_first(self, obs_log):
        await obs_log.log("event", "src", details={"order": 1})
        await obs_log.log("event", "src", details={"order": 2})
        await obs_log.log("event", "src", details={"order": 3})
        results = await obs_log.query(event_type="event")
        assert results[0]["details"]["order"] == 3

    async def test_get_by_task(self, obs_log):
        await obs_log.log("event", "src", task_id="t1")
        await obs_log.log("event", "src", task_id="t1")
        await obs_log.log("event", "src", task_id="t2")
        results = await obs_log.get_by_task("t1")
        assert len(results) == 2

    async def test_get_by_agent(self, obs_log):
        await obs_log.log("event", "src", agent_id="a1")
        await obs_log.log("event", "src", agent_id="a1")
        await obs_log.log("event", "src", agent_id="a2")
        results = await obs_log.get_by_agent("a1")
        assert len(results) == 2

    async def test_count_events(self, obs_log):
        await obs_log.log("type_a", "src")
        await obs_log.log("type_a", "src")
        await obs_log.log("type_b", "src")
        assert await obs_log.count_events(event_type="type_a") == 2
        assert await obs_log.count_events() == 3

    async def test_count_events_with_since(self, obs_log):
        before = time.time()
        await obs_log.log("event", "src")
        assert await obs_log.count_events(since=before) == 1
        assert await obs_log.count_events(since=before + 1000) == 0

    async def test_export_logs(self, obs_log):
        await obs_log.log("event_1", "src")
        await obs_log.log("event_2", "src")
        exported = await obs_log.export_logs()
        assert len(exported) == 2
