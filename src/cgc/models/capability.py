from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from cgc.models.agent import AgentLevel, AgentRole
from cgc.models.task import TaskStage


class CapabilityStrength(str, Enum):
    READ_ONLY = "READ_ONLY"
    PROPOSE = "PROPOSE"
    PREVIEW = "PREVIEW"
    APPLY_SCOPED = "APPLY_SCOPED"
    APPLY_FULL = "APPLY_FULL"


@dataclass
class CapabilityHandle:
    name: str
    strength: CapabilityStrength
    constraints: dict[str, Any]
    description: str


@dataclass
class CapabilityTable:
    agent_id: str
    handles: list[CapabilityHandle]
    valid_until: float | None
    context_hash: str


@dataclass
class CapabilityRule:
    required_level: AgentLevel
    required_roles: list[AgentRole]
    allowed_strengths: list[CapabilityStrength]
    task_stages: list[TaskStage]
    max_strength: CapabilityStrength
