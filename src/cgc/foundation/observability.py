"""Unified behavioral recording layer for the CGC system.

Records message flow, dispatch decisions, agent task acceptance,
capability table refreshes, capability requests, artifact writes,
discipline suggestions, elevation events, constitution reviews,
and human feedback.
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from typing import Any


class ObservabilityLog:
    """Async-safe, append-only log that captures every meaningful event
    produced by the Computational Governance fabric.

    All public methods are coroutines and guarded by an
    :class:`asyncio.Lock` so concurrent coroutines can safely share a
    single instance.
    """

    def __init__(self) -> None:
        self._logs: list[dict[str, Any]] = []
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def log(
        self,
        event_type: str,
        source: str,
        details: dict[str, Any] | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        """Append a structured log entry and return its ``log_id``.

        Parameters
        ----------
        event_type:
            Category of the event (e.g. ``"message_flow"``,
            ``"dispatch_decision"``, ``"agent_task_acceptance"``).
        source:
            Identifier of the component or agent that emitted the event.
        details:
            Arbitrary key/value payload for the event.
        agent_id:
            Optional agent identifier associated with the event.
        task_id:
            Optional task identifier associated with the event.
        """
        log_id = uuid.uuid4().hex
        entry: dict[str, Any] = {
            "log_id": log_id,
            "event_type": event_type,
            "source": source,
            "details": details if details is not None else {},
            "agent_id": agent_id,
            "task_id": task_id,
            "timestamp": time.time(),
        }
        async with self._lock:
            self._logs.append(entry)
        return log_id

    # ------------------------------------------------------------------
    # Query path
    # ------------------------------------------------------------------

    async def query(
        self,
        event_type: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        source: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return log entries matching *all* provided filters.

        Parameters
        ----------
        event_type:
            Filter by event category.
        agent_id:
            Filter by agent identifier.
        task_id:
            Filter by task identifier.
        source:
            Filter by source component.
        since:
            Inclusive lower bound on the timestamp (epoch seconds).
        until:
            Inclusive upper bound on the timestamp (epoch seconds).
        limit:
            Maximum number of entries to return (most recent first).
        """
        async with self._lock:
            results: list[dict[str, Any]] = []
            for entry in self._logs:
                if event_type is not None and entry["event_type"] != event_type:
                    continue
                if agent_id is not None and entry["agent_id"] != agent_id:
                    continue
                if task_id is not None and entry["task_id"] != task_id:
                    continue
                if source is not None and entry["source"] != source:
                    continue
                if since is not None and entry["timestamp"] < since:
                    continue
                if until is not None and entry["timestamp"] > until:
                    continue
                results.append(copy.copy(entry))
        # Return most-recent-first, bounded by limit.
        results.reverse()
        return results[:limit]

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    async def get_by_task(self, task_id: str) -> list[dict[str, Any]]:
        """Return all log entries associated with *task_id*."""
        return await self.query(task_id=task_id, limit=1_000_000)

    async def get_by_agent(self, agent_id: str) -> list[dict[str, Any]]:
        """Return all log entries associated with *agent_id*."""
        return await self.query(agent_id=agent_id, limit=1_000_000)

    async def count_events(
        self,
        event_type: str | None = None,
        since: float | None = None,
    ) -> int:
        """Count log entries, optionally filtered by *event_type* and/or
        a *since* timestamp."""
        async with self._lock:
            count = 0
            for entry in self._logs:
                if event_type is not None and entry["event_type"] != event_type:
                    continue
                if since is not None and entry["timestamp"] < since:
                    continue
                count += 1
            return count

    async def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the *limit* most recent log entries."""
        async with self._lock:
            tail = self._logs[-limit:]
        # Return newest-first.
        result = [copy.copy(e) for e in tail]
        result.reverse()
        return result

    async def export_logs(self) -> list[dict[str, Any]]:
        """Return a shallow copy of the complete log for audit / replay."""
        async with self._lock:
            return list(self._logs)
