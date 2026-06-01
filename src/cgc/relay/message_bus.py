"""Central message hub for the CGC system.

The :class:`MessageBus` is the single exchange point through which all
components communicate.  It maintains an async queue, a typed subscriber
registry, and an append-only message history.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable, Coroutine

from cgc.models.messages import Message, MessageType


class MessageBus:
    """Async-safe pub/sub message hub.

    *   Publishers call :meth:`publish` to push a :class:`Message` onto the
        internal queue and fan it out to every registered subscriber.
    *   Consumers may either *subscribe* (push model) or *pull* from the
        queue via :meth:`receive`.
    *   Every message is stored in an in-memory history list so that
        components can query past traffic without replaying the queue.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Message] = asyncio.Queue()
        self._subscribers: dict[str, list[Callable[[Message], Coroutine[Any, Any, None]]]] = {}
        self._history: list[Message] = []
        self._lock: asyncio.Lock = asyncio.Lock()
        # Internal bookkeeping for unsubscribe look-ups.
        self._subscription_index: dict[str, tuple[str | None, Callable[[Message], Coroutine[Any, Any, None]]]] = {}

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, message: Message) -> str:
        """Publish *message* to the queue and notify subscribers.

        The message is also appended to the internal history so it can be
        retrieved later via the ``get_*`` helpers.

        Returns
        -------
        str
            The ``msg_id`` of the published message.
        """
        msg_id = message.msg_id

        # Store in history under lock.
        async with self._lock:
            self._history.append(message)

        # Enqueue for pull consumers.
        await self._queue.put(message)

        # Fan out to subscribers.  Wildcard subscribers (registered with
        # ``msg_type=None``) live under the ``None`` key.
        type_key = message.msg_type.value
        for key in (type_key, None):
            callbacks = self._subscribers.get(key, [])
            for callback in callbacks:
                # Fire-and-forget -- we do not await the callback so a
                # slow subscriber cannot block the publisher.  Each
                # invocation is scheduled as its own task.
                asyncio.ensure_future(callback(message))

        return msg_id

    # ------------------------------------------------------------------
    # Subscribe / Unsubscribe
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        msg_type: MessageType | None,
        callback: Callable[[Message], Coroutine[Any, Any, None]],
    ) -> str:
        """Register *callback* for messages of *msg_type*.

        If *msg_type* is ``None`` the callback receives **all** message
        types (wildcard subscription).

        Returns
        -------
        str
            A ``subscription_id`` that can be passed to :meth:`unsubscribe`.
        """
        subscription_id = uuid.uuid4().hex
        type_key: str | None = msg_type.value if msg_type is not None else None

        async with self._lock:
            self._subscribers.setdefault(type_key, []).append(callback)
            self._subscription_index[subscription_id] = (type_key, callback)

        return subscription_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a previously registered subscription.

        Returns ``True`` if the subscription existed and was removed,
        ``False`` otherwise.
        """
        async with self._lock:
            entry = self._subscription_index.pop(subscription_id, None)
            if entry is None:
                return False
            type_key, callback = entry
            callbacks = self._subscribers.get(type_key, [])
            try:
                callbacks.remove(callback)
            except ValueError:
                pass  # Already removed or never present.
            if not callbacks:
                self._subscribers.pop(type_key, None)
        return True

    # ------------------------------------------------------------------
    # Pull-based receive
    # ------------------------------------------------------------------

    async def receive(self, timeout: float = 0.1) -> Message | None:
        """Pull the next message from the queue.

        Waits up to *timeout* seconds for a message to arrive.  Returns
        ``None`` if the queue is empty after the timeout elapses.
        """
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------------
    # History queries
    # ------------------------------------------------------------------

    async def get_message(self, msg_id: str) -> Message | None:
        """Look up a single message by its ``msg_id``."""
        async with self._lock:
            for msg in self._history:
                if msg.msg_id == msg_id:
                    return msg
        return None

    async def get_messages_by_task(self, task_id: str) -> list[Message]:
        """Return all messages associated with *task_id*."""
        async with self._lock:
            return [m for m in self._history if m.task_id == task_id]

    async def get_messages_by_type(self, msg_type: MessageType) -> list[Message]:
        """Return all messages of the given *msg_type*."""
        type_value = msg_type.value
        async with self._lock:
            return [m for m in self._history if m.msg_type.value == type_value]

    async def get_recent(self, limit: int = 50) -> list[Message]:
        """Return the *limit* most recent messages (newest last)."""
        async with self._lock:
            return list(self._history[-limit:])
