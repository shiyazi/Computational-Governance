"""Tests for CGC relay protocol: MessageBus and Dispatcher."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.foundation.task_state import TaskStateCore
from cgc.models.agent import AgentLevel, AgentProfile, AgentRole, AgentStatus
from cgc.models.messages import Message, MessageType
from cgc.relay.dispatcher import Dispatcher
from cgc.relay.message_bus import MessageBus


# ======================================================================
# Fixtures
# ======================================================================

@pytest_asyncio.fixture
async def bus():
    return MessageBus()


@pytest_asyncio.fixture
async def task_core():
    return TaskStateCore()


@pytest_asyncio.fixture
async def registry():
    return AgentRegistry()


@pytest_asyncio.fixture
async def obs():
    return ObservabilityLog()


@pytest_asyncio.fixture
async def dispatcher(bus, task_core, registry, obs):
    return Dispatcher(bus, task_core, registry, None, obs)


@pytest_asyncio.fixture
async def registered_agents(registry):
    """Register three agents with different roles and levels."""
    planner = AgentProfile.create(
        name="Planner", role=AgentRole.PLANNER, level=AgentLevel.SENIOR, load=0,
    )
    executor = AgentProfile.create(
        name="Executor", role=AgentRole.EXECUTOR, level=AgentLevel.INTERMEDIATE, load=0,
    )
    reviewer = AgentProfile.create(
        name="Reviewer", role=AgentRole.REVIEWER, level=AgentLevel.SENIOR, load=0,
    )
    await registry.register(planner)
    await registry.register(executor)
    await registry.register(reviewer)
    return {"planner": planner, "executor": executor, "reviewer": reviewer}


# ======================================================================
# MessageBus tests
# ======================================================================

@pytest.mark.asyncio
class TestMessageBus:

    async def test_publish_returns_msg_id(self, bus):
        msg = Message.create(
            msg_type=MessageType.TASK_SUBMIT,
            sender="test",
        )
        msg_id = await bus.publish(msg)
        assert msg_id == msg.msg_id

    async def test_subscribe_callback_receives_message(self, bus):
        received: list[Message] = []

        async def callback(m: Message):
            received.append(m)

        await bus.subscribe(MessageType.TASK_SUBMIT, callback)
        msg = Message.create(msg_type=MessageType.TASK_SUBMIT, sender="test")
        await bus.publish(msg)
        # Give the callback task a moment to execute
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].msg_id == msg.msg_id

    async def test_subscribe_wildcard_receives_all(self, bus):
        received: list[Message] = []

        async def callback(m: Message):
            received.append(m)

        await bus.subscribe(None, callback)
        msg1 = Message.create(msg_type=MessageType.TASK_SUBMIT, sender="test")
        msg2 = Message.create(msg_type=MessageType.TASK_RESULT, sender="test")
        await bus.publish(msg1)
        await bus.publish(msg2)
        await asyncio.sleep(0.05)
        assert len(received) == 2

    async def test_subscribe_type_filtered(self, bus):
        received: list[Message] = []

        async def callback(m: Message):
            received.append(m)

        await bus.subscribe(MessageType.TASK_SUBMIT, callback)
        msg1 = Message.create(msg_type=MessageType.TASK_SUBMIT, sender="test")
        msg2 = Message.create(msg_type=MessageType.TASK_RESULT, sender="test")
        await bus.publish(msg1)
        await bus.publish(msg2)
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].msg_type == MessageType.TASK_SUBMIT

    async def test_unsubscribe(self, bus):
        received: list[Message] = []

        async def callback(m: Message):
            received.append(m)

        sub_id = await bus.subscribe(MessageType.TASK_SUBMIT, callback)
        msg1 = Message.create(msg_type=MessageType.TASK_SUBMIT, sender="test")
        await bus.publish(msg1)
        await asyncio.sleep(0.05)

        result = await bus.unsubscribe(sub_id)
        assert result is True

        msg2 = Message.create(msg_type=MessageType.TASK_SUBMIT, sender="test")
        await bus.publish(msg2)
        await asyncio.sleep(0.05)
        # Should only have received the first message
        assert len(received) == 1

    async def test_unsubscribe_not_found(self, bus):
        result = await bus.unsubscribe("nonexistent")
        assert result is False

    async def test_receive(self, bus):
        msg = Message.create(msg_type=MessageType.TASK_SUBMIT, sender="test")
        await bus.publish(msg)
        received = await bus.receive(timeout=1.0)
        assert received is not None
        assert received.msg_id == msg.msg_id

    async def test_receive_timeout(self, bus):
        received = await bus.receive(timeout=0.05)
        assert received is None

    async def test_get_message(self, bus):
        msg = Message.create(msg_type=MessageType.TASK_SUBMIT, sender="test")
        await bus.publish(msg)
        found = await bus.get_message(msg.msg_id)
        assert found is not None
        assert found.msg_id == msg.msg_id

    async def test_get_message_not_found(self, bus):
        found = await bus.get_message("nonexistent")
        assert found is None

    async def test_get_messages_by_task(self, bus):
        msg1 = Message.create(
            msg_type=MessageType.TASK_SUBMIT, sender="test", task_id="task_1",
        )
        msg2 = Message.create(
            msg_type=MessageType.TASK_ASSIGN, sender="test", task_id="task_1",
        )
        msg3 = Message.create(
            msg_type=MessageType.TASK_SUBMIT, sender="test", task_id="task_2",
        )
        await bus.publish(msg1)
        await bus.publish(msg2)
        await bus.publish(msg3)
        results = await bus.get_messages_by_task("task_1")
        assert len(results) == 2

    async def test_get_messages_by_type(self, bus):
        msg1 = Message.create(msg_type=MessageType.TASK_SUBMIT, sender="test")
        msg2 = Message.create(msg_type=MessageType.TASK_RESULT, sender="test")
        await bus.publish(msg1)
        await bus.publish(msg2)
        results = await bus.get_messages_by_type(MessageType.TASK_SUBMIT)
        assert len(results) == 1


# ======================================================================
# Dispatcher tests
# ======================================================================

@pytest.mark.asyncio
class TestDispatcher:

    async def test_submit_task(self, dispatcher, task_core):
        task = await dispatcher.submit_task("task_1", "Build feature X")
        assert task.task_id == "task_1"
        assert task.stage == "PENDING"
        stored = await task_core.get_task("task_1")
        assert stored is not None

    async def test_assign_task(self, dispatcher, task_core, registry, registered_agents):
        await dispatcher.submit_task("task_1", "Build feature X")
        agent = registered_agents["executor"]
        msg = await dispatcher.assign_task("task_1", agent.agent_id)

        task = await task_core.get_task("task_1")
        assert task.owner == agent.agent_id
        assert task.stage == "DISPATCHED"
        assert agent.agent_id in task.participants
        assert msg.msg_type == MessageType.TASK_ASSIGN

    async def test_receive_result(self, dispatcher, task_core, registry, registered_agents):
        await dispatcher.submit_task("task_1", "Build feature X")
        agent = registered_agents["executor"]
        await dispatcher.assign_task("task_1", agent.agent_id)

        msg = await dispatcher.receive_result(
            agent.agent_id, "task_1", {"status": "done", "output": "result"},
        )
        assert msg.msg_type == MessageType.TASK_RESULT

        task = await task_core.get_task("task_1")
        assert task.turn == 1

    async def test_delegate_subtask(self, dispatcher, task_core, registry, registered_agents):
        parent_agent = registered_agents["planner"]
        child_agent = registered_agents["executor"]

        await dispatcher.submit_task("parent_1", "Parent task")
        await dispatcher.assign_task("parent_1", parent_agent.agent_id)

        msg = await dispatcher.delegate_subtask(
            parent_task_id="parent_1",
            child_task_id="child_1",
            target_agent_id=child_agent.agent_id,
            delegation_spec={"description": "Sub task"},
        )

        assert msg.msg_type == MessageType.TASK_DELEGATE

        parent = await task_core.get_task("parent_1")
        assert "child_1" in parent.child_task_ids

        child = await task_core.get_task("child_1")
        assert child is not None
        assert child.parent_task_id == "parent_1"
        assert child.owner == child_agent.agent_id
        assert child.stage == "DISPATCHED"

    async def test_select_agent_picks_lowest_load(self, dispatcher, registry):
        low_load = AgentProfile.create(
            name="LowLoad", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR, load=0,
        )
        high_load = AgentProfile.create(
            name="HighLoad", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR, load=5,
        )
        await registry.register(low_load)
        await registry.register(high_load)

        selected = await dispatcher.select_agent(role=AgentRole.EXECUTOR)
        assert selected is not None
        assert selected.agent_id == low_load.agent_id

    async def test_select_agent_with_min_level(self, dispatcher, registry):
        junior = AgentProfile.create(
            name="Junior", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR, load=0,
        )
        senior = AgentProfile.create(
            name="Senior", role=AgentRole.EXECUTOR, level=AgentLevel.SENIOR, load=0,
        )
        await registry.register(junior)
        await registry.register(senior)

        selected = await dispatcher.select_agent(min_level=AgentLevel.SENIOR)
        assert selected is not None
        assert selected.agent_id == senior.agent_id

    async def test_select_agent_with_exclude(self, dispatcher, registry):
        a1 = AgentProfile.create(name="A1", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR, load=0)
        a2 = AgentProfile.create(name="A2", role=AgentRole.EXECUTOR, level=AgentLevel.JUNIOR, load=0)
        await registry.register(a1)
        await registry.register(a2)

        selected = await dispatcher.select_agent(exclude=[a1.agent_id])
        assert selected is not None
        assert selected.agent_id == a2.agent_id

    async def test_select_agent_none_available(self, dispatcher):
        selected = await dispatcher.select_agent()
        assert selected is None

    async def test_advance_task_stage(self, dispatcher, task_core):
        from cgc.models.task import TaskStage
        await dispatcher.submit_task("task_1", "Test")
        task = await dispatcher.advance_task_stage("task_1", TaskStage.EXECUTING)
        assert task.stage == TaskStage.EXECUTING
