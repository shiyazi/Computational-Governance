"""Single source of truth for all task state in the CGC system."""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

from cgc.models.task import TaskStage, TaskState

# Terminal stages – tasks in these stages are considered finished.
_TERMINAL_STAGES: frozenset[TaskStage] = frozenset(
    {TaskStage.COMPLETED, TaskStage.FAILED}
)


class TaskStateCore:
    """Thread-safe, async-aware store for :class:`TaskState` objects.

    Every mutation acquires an :class:`asyncio.Lock` so that concurrent
    coroutines can safely share a single instance.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskState] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # CRUD helpers
    # ------------------------------------------------------------------

    async def create_task(
        self,
        task_id: str,
        parent_task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskState:
        """Create a new task in the :attr:`TaskStage.PENDING` stage."""
        now = time.time()
        state = TaskState(
            task_id=task_id,
            parent_task_id=parent_task_id,
            child_task_ids=[],
            stage=TaskStage.PENDING,
            round_num=0,
            turn=0,
            owner=None,
            participants=[],
            constraints={},
            artifact_refs=[],
            in_governance=False,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        async with self._lock:
            self._tasks[task_id] = state
        return state

    async def get_task(self, task_id: str) -> TaskState | None:
        """Retrieve a task by ID, returning ``None`` when absent."""
        async with self._lock:
            return self._tasks.get(task_id)

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task.  Returns ``True`` if the task existed."""
        async with self._lock:
            return self._tasks.pop(task_id, None) is not None

    # ------------------------------------------------------------------
    # Field mutators – all return the *updated* TaskState
    # ------------------------------------------------------------------

    async def update_stage(self, task_id: str, stage: TaskStage) -> TaskState:
        """Set the stage and touch ``updated_at``."""
        async with self._lock:
            task = self._require(task_id)
            task.stage = stage
            task.updated_at = time.time()
            return task

    async def advance_turn(self, task_id: str) -> TaskState:
        """Increment ``turn`` by one."""
        async with self._lock:
            task = self._require(task_id)
            task.turn += 1
            task.updated_at = time.time()
            return task

    async def advance_round(self, task_id: str) -> TaskState:
        """Increment ``round_num`` by one."""
        async with self._lock:
            task = self._require(task_id)
            task.round_num += 1
            task.updated_at = time.time()
            return task

    async def set_owner(self, task_id: str, owner: str) -> TaskState:
        async with self._lock:
            task = self._require(task_id)
            task.owner = owner
            task.updated_at = time.time()
            return task

    async def add_participant(self, task_id: str, agent_id: str) -> TaskState:
        async with self._lock:
            task = self._require(task_id)
            if agent_id not in task.participants:
                task.participants.append(agent_id)
            task.updated_at = time.time()
            return task

    async def add_child_task(self, parent_id: str, child_id: str) -> TaskState:
        async with self._lock:
            task = self._require(parent_id)
            if child_id not in task.child_task_ids:
                task.child_task_ids.append(child_id)
            task.updated_at = time.time()
            return task

    async def add_artifact_ref(self, task_id: str, ref: str) -> TaskState:
        async with self._lock:
            task = self._require(task_id)
            if ref not in task.artifact_refs:
                task.artifact_refs.append(ref)
            task.updated_at = time.time()
            return task

    async def set_governance_hold(self, task_id: str, hold: bool) -> TaskState:
        async with self._lock:
            task = self._require(task_id)
            task.in_governance = hold
            task.updated_at = time.time()
            return task

    async def set_constraints(
        self, task_id: str, constraints: dict[str, Any]
    ) -> TaskState:
        async with self._lock:
            task = self._require(task_id)
            task.constraints = constraints
            task.updated_at = time.time()
            return task

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def get_active_tasks(self) -> list[TaskState]:
        """Return all tasks that are **not** in a terminal stage."""
        async with self._lock:
            return [
                copy.copy(t)
                for t in self._tasks.values()
                if t.stage not in _TERMINAL_STAGES
            ]

    async def get_tasks_by_owner(self, owner: str) -> list[TaskState]:
        async with self._lock:
            return [
                copy.copy(t) for t in self._tasks.values() if t.owner == owner
            ]

    async def get_child_tasks(self, parent_id: str) -> list[TaskState]:
        async with self._lock:
            return [
                copy.copy(t)
                for t in self._tasks.values()
                if t.parent_task_id == parent_id
            ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require(self, task_id: str) -> TaskState:
        """Return the task or raise :class:`KeyError`."""
        try:
            return self._tasks[task_id]
        except KeyError:
            raise KeyError(f"Task not found: {task_id}") from None
