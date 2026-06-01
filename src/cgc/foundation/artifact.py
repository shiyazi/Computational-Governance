"""Artifact store for all process artifacts in the CGC system."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from typing import Any


class ArtifactStore:
    """In-memory, async-safe store for process artifacts.

    Artifacts (plans, subtask proposals, analysis reports, patches, review
    notes, test results, etc.) are stored here so that messages only need to
    pass references instead of full content.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def store(
        self,
        artifact_id: str | None,
        artifact_type: str,
        content: Any,
        metadata: dict[str, Any] | None = None,
        producer: str | None = None,
        task_id: str | None = None,
    ) -> str:
        """Store an artifact and return its ID.

        If *artifact_id* is ``None`` a UUID4 is generated automatically.
        """
        if artifact_id is None:
            artifact_id = uuid.uuid4().hex

        record: dict[str, Any] = {
            "id": artifact_id,
            "type": artifact_type,
            "content": content,
            "metadata": metadata or {},
            "producer": producer,
            "task_id": task_id,
            "created_at": time.time(),
        }

        async with self._lock:
            self._store[artifact_id] = record

        return artifact_id

    async def get(self, artifact_id: str) -> dict[str, Any] | None:
        """Retrieve a single artifact by ID, returning ``None`` when absent."""
        async with self._lock:
            record = self._store.get(artifact_id)
            return copy.deepcopy(record) if record is not None else None

    async def get_by_task(self, task_id: str) -> list[dict[str, Any]]:
        """Return all artifacts associated with *task_id*."""
        async with self._lock:
            return [
                copy.deepcopy(r)
                for r in self._store.values()
                if r.get("task_id") == task_id
            ]

    async def get_by_producer(self, producer: str) -> list[dict[str, Any]]:
        """Return all artifacts produced by *producer*."""
        async with self._lock:
            return [
                copy.deepcopy(r)
                for r in self._store.values()
                if r.get("producer") == producer
            ]

    async def get_by_type(self, artifact_type: str) -> list[dict[str, Any]]:
        """Return all artifacts of the given *artifact_type*."""
        async with self._lock:
            return [
                copy.deepcopy(r)
                for r in self._store.values()
                if r.get("type") == artifact_type
            ]

    async def update(
        self,
        artifact_id: str,
        content: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update an existing artifact's content and/or metadata.

        Returns the updated record, or ``None`` if the artifact does not exist.
        Only the fields that are explicitly provided are changed.
        """
        async with self._lock:
            record = self._store.get(artifact_id)
            if record is None:
                return None

            if content is not None:
                record["content"] = content
            if metadata is not None:
                record["metadata"] = metadata

            return copy.deepcopy(record)

    async def delete(self, artifact_id: str) -> bool:
        """Delete an artifact.  Returns ``True`` if it existed."""
        async with self._lock:
            return self._store.pop(artifact_id, None) is not None

    async def list_all(self) -> list[dict[str, Any]]:
        """Return a shallow copy of every stored artifact."""
        async with self._lock:
            return [copy.deepcopy(r) for r in self._store.values()]
