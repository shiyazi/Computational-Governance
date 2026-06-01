"""Relay layer -- message network and dispatch control for the CGC system."""

from __future__ import annotations

from cgc.relay.dispatcher import Dispatcher
from cgc.relay.message_bus import MessageBus

__all__ = [
    "Dispatcher",
    "MessageBus",
]
