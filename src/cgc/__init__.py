"""Computational Governance Core (CGC)."""

from __future__ import annotations

__version__ = "0.1.0"

# -- Engine ----------------------------------------------------------------
from cgc.engine import CGCEngine

# -- Foundation layer -------------------------------------------------------
from cgc.foundation.artifact import ArtifactStore
from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.registry import AgentRegistry
from cgc.foundation.task_state import TaskStateCore

# -- Relay layer ------------------------------------------------------------
from cgc.relay.dispatcher import Dispatcher
from cgc.relay.message_bus import MessageBus

# -- Authority layer --------------------------------------------------------
from cgc.authority.capability_network import CapabilityNetwork
from cgc.authority.projection import ProjectionEngine

# -- Governance layer -------------------------------------------------------
from cgc.governance.constitution import ConstitutionEngine
from cgc.governance.discipline import DisciplineSystem
from cgc.governance.elevation import ElevationEngine
from cgc.governance.feedback import FeedbackAttributionLayer
from cgc.governance.reputation import ReputationRating

# -- Models -----------------------------------------------------------------
from cgc.models.agent import (
    AgentLevel,
    AgentProfile,
    AgentRole,
    AgentStatus,
    ContractType,
)
from cgc.models.capability import (
    CapabilityHandle,
    CapabilityRule,
    CapabilityStrength,
    CapabilityTable,
)
from cgc.models.governance import (
    ConstitutionAppeal,
    ConstitutionRuling,
    ConstitutionVerdict,
    DisciplineAction,
    DisciplineReasonCode,
    DisciplineSuggestion,
    ElevationDecision,
    ElevationRequest,
    FeedbackAttribution,
    FeedbackCategory,
    ReputationProfile,
)
from cgc.models.messages import (
    CapabilityRequest,
    GovernanceMessage,
    Message,
    MessageType,
    TaskMessage,
)
from cgc.models.task import TaskStage, TaskState

__all__ = [
    "__version__",
    # Engine
    "CGCEngine",
    # Foundation
    "ArtifactStore",
    "ObservabilityLog",
    "AgentRegistry",
    "TaskStateCore",
    # Relay
    "Dispatcher",
    "MessageBus",
    # Authority
    "CapabilityNetwork",
    "ProjectionEngine",
    # Governance
    "ConstitutionEngine",
    "DisciplineSystem",
    "ElevationEngine",
    "FeedbackAttributionLayer",
    "ReputationRating",
    # Agent
    "AgentLevel",
    "AgentProfile",
    "AgentRole",
    "AgentStatus",
    "ContractType",
    # Capability
    "CapabilityHandle",
    "CapabilityRule",
    "CapabilityStrength",
    "CapabilityTable",
    # Governance models
    "ConstitutionAppeal",
    "ConstitutionRuling",
    "ConstitutionVerdict",
    "DisciplineAction",
    "DisciplineReasonCode",
    "DisciplineSuggestion",
    "ElevationDecision",
    "ElevationRequest",
    "FeedbackAttribution",
    "FeedbackCategory",
    "ReputationProfile",
    # Messages
    "CapabilityRequest",
    "GovernanceMessage",
    "Message",
    "MessageType",
    "TaskMessage",
    # Task
    "TaskStage",
    "TaskState",
]
