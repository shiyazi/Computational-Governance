from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    TASK_SUBMIT = "TASK_SUBMIT"
    TASK_ASSIGN = "TASK_ASSIGN"
    TASK_RESULT = "TASK_RESULT"
    TASK_DELEGATE = "TASK_DELEGATE"
    CAPABILITY_REQUEST = "CAPABILITY_REQUEST"
    CAPABILITY_REFRESH = "CAPABILITY_REFRESH"
    GOVERNANCE_EVENT = "GOVERNANCE_EVENT"
    DISCIPLINE_TRIGGER = "DISCIPLINE_TRIGGER"
    ELEVATION_TRIGGER = "ELEVATION_TRIGGER"
    FEEDBACK_HUMAN = "FEEDBACK_HUMAN"
    CONSTITUTION_APPEAL = "CONSTITUTION_APPEAL"
    LOG_EVENT = "LOG_EVENT"
    SCHEDULER_TICK = "SCHEDULER_TICK"


@dataclass
class Message:
    msg_id: str
    msg_type: MessageType
    sender: str
    receiver: str | None
    payload: dict[str, Any]
    timestamp: float
    task_id: str | None
    correlation_id: str | None

    @classmethod
    def create(
        cls,
        msg_type: MessageType,
        sender: str,
        receiver: str | None = None,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Message:
        return cls(
            msg_id=uuid.uuid4().hex,
            msg_type=msg_type,
            sender=sender,
            receiver=receiver,
            payload=payload or {},
            timestamp=time.time(),
            task_id=task_id,
            correlation_id=correlation_id,
        )


@dataclass
class TaskMessage(Message):
    task_action: str = ""
    task_payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    deadline: float | None = None
    retry_count: int = 0
    max_retries: int = 3

    @classmethod
    def create(
        cls,
        msg_type: MessageType,
        sender: str,
        task_action: str,
        receiver: str | None = None,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        task_payload: dict[str, Any] | None = None,
        priority: int = 0,
        deadline: float | None = None,
        retry_count: int = 0,
        max_retries: int = 3,
    ) -> TaskMessage:
        return cls(
            msg_id=uuid.uuid4().hex,
            msg_type=msg_type,
            sender=sender,
            receiver=receiver,
            payload=payload or {},
            timestamp=time.time(),
            task_id=task_id,
            correlation_id=correlation_id,
            task_action=task_action,
            task_payload=task_payload or {},
            priority=priority,
            deadline=deadline,
            retry_count=retry_count,
            max_retries=max_retries,
        )


@dataclass
class GovernanceMessage(Message):
    governance_type: str = ""
    severity: str = "info"
    authority_id: str | None = None
    affected_agents: list[str] = field(default_factory=list)
    ruling: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        msg_type: MessageType,
        sender: str,
        governance_type: str,
        receiver: str | None = None,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        severity: str = "info",
        authority_id: str | None = None,
        affected_agents: list[str] | None = None,
        ruling: dict[str, Any] | None = None,
    ) -> GovernanceMessage:
        return cls(
            msg_id=uuid.uuid4().hex,
            msg_type=msg_type,
            sender=sender,
            receiver=receiver,
            payload=payload or {},
            timestamp=time.time(),
            task_id=task_id,
            correlation_id=correlation_id,
            governance_type=governance_type,
            severity=severity,
            authority_id=authority_id,
            affected_agents=affected_agents or [],
            ruling=ruling or {},
        )


@dataclass
class CapabilityRequest(Message):
    agent_id: str = ""
    requested_capabilities: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        sender: str,
        agent_id: str,
        requested_capabilities: list[str],
        context: dict[str, Any] | None = None,
        receiver: str | None = None,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CapabilityRequest:
        return cls(
            msg_id=uuid.uuid4().hex,
            msg_type=MessageType.CAPABILITY_REQUEST,
            sender=sender,
            receiver=receiver,
            payload=payload or {},
            timestamp=time.time(),
            task_id=task_id,
            correlation_id=correlation_id,
            agent_id=agent_id,
            requested_capabilities=requested_capabilities,
            context=context or {},
        )
