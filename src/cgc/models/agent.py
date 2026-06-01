from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentLevel(int, Enum):
    NOVICE = 1
    JUNIOR = 2
    INTERMEDIATE = 3
    SENIOR = 4
    PRINCIPAL = 5


class AgentRole(str, Enum):
    PLANNER = "PLANNER"
    EXECUTOR = "EXECUTOR"
    REVIEWER = "REVIEWER"
    EXTERNAL_EXECUTOR = "EXTERNAL_EXECUTOR"


class AgentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    RETIRED = "RETIRED"
    OBSERVATION = "OBSERVATION"


class ContractType(str, Enum):
    CONTROLLED = "CONTROLLED"
    EXTERNAL = "EXTERNAL"


@dataclass
class AgentProfile:
    agent_id: str
    name: str
    role: AgentRole
    level: AgentLevel
    contract_type: ContractType
    status: AgentStatus
    load: int
    current_task_ids: list[str]
    capabilities: list[str]
    metadata: dict[str, Any]
    registered_at: float

    @classmethod
    def create(
        cls,
        name: str,
        role: AgentRole,
        level: AgentLevel,
        contract_type: ContractType = ContractType.CONTROLLED,
        status: AgentStatus = AgentStatus.ACTIVE,
        load: int = 0,
        current_task_ids: list[str] | None = None,
        capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentProfile:
        return cls(
            agent_id=uuid.uuid4().hex,
            name=name,
            role=role,
            level=level,
            contract_type=contract_type,
            status=status,
            load=load,
            current_task_ids=current_task_ids or [],
            capabilities=capabilities or [],
            metadata=metadata or {},
            registered_at=time.time(),
        )
