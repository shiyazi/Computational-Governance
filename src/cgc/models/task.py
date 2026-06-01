from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStage(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    DISPATCHED = "DISPATCHED"
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    GOVERNANCE_HOLD = "GOVERNANCE_HOLD"


@dataclass
class TaskState:
    task_id: str
    parent_task_id: str | None
    child_task_ids: list[str]
    stage: TaskStage
    round_num: int
    turn: int
    owner: str | None
    participants: list[str]
    constraints: dict[str, Any]
    artifact_refs: list[str]
    in_governance: bool
    created_at: float
    updated_at: float
    metadata: dict[str, Any]

    @classmethod
    def create(
        cls,
        parent_task_id: str | None = None,
        child_task_ids: list[str] | None = None,
        stage: TaskStage = TaskStage.PENDING,
        round_num: int = 0,
        turn: int = 0,
        owner: str | None = None,
        participants: list[str] | None = None,
        constraints: dict[str, Any] | None = None,
        artifact_refs: list[str] | None = None,
        in_governance: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> TaskState:
        now = time.time()
        return cls(
            task_id=uuid.uuid4().hex,
            parent_task_id=parent_task_id,
            child_task_ids=child_task_ids or [],
            stage=stage,
            round_num=round_num,
            turn=turn,
            owner=owner,
            participants=participants or [],
            constraints=constraints or {},
            artifact_refs=artifact_refs or [],
            in_governance=in_governance,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
