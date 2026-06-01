"""Projection engine for the CGC Authority Protocol (Capability Network).

Determines what capabilities an agent can access based on its profile,
task context, and configured capability rules.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cgc.models import (
    AgentLevel,
    AgentProfile,
    AgentRole,
    CapabilityHandle,
    CapabilityRule,
    CapabilityStrength,
    TaskStage,
    TaskState,
)


# Strength ordering from weakest to strongest.
_STRENGTH_ORDER: dict[CapabilityStrength, int] = {
    CapabilityStrength.READ_ONLY: 0,
    CapabilityStrength.PROPOSE: 1,
    CapabilityStrength.PREVIEW: 2,
    CapabilityStrength.APPLY_SCOPED: 3,
    CapabilityStrength.APPLY_FULL: 4,
}


def _strength_rank(strength: CapabilityStrength) -> int:
    return _STRENGTH_ORDER[strength]


def _level_permitted_strength(level: AgentLevel) -> CapabilityStrength:
    """Return the maximum default strength an agent level permits."""
    if level <= AgentLevel.NOVICE:
        return CapabilityStrength.READ_ONLY
    if level <= AgentLevel.JUNIOR:
        return CapabilityStrength.PROPOSE
    if level <= AgentLevel.INTERMEDIATE:
        return CapabilityStrength.APPLY_SCOPED
    # SENIOR and PRINCIPAL
    return CapabilityStrength.APPLY_FULL


def _min_strength(a: CapabilityStrength, b: CapabilityStrength) -> CapabilityStrength:
    """Return the weaker of two strengths."""
    if _strength_rank(a) <= _strength_rank(b):
        return a
    return b


def _clamp_strength(strength: CapabilityStrength, ceiling: CapabilityStrength) -> CapabilityStrength:
    """Return *strength* if it does not exceed *ceiling*, else *ceiling*."""
    if _strength_rank(strength) <= _strength_rank(ceiling):
        return strength
    return ceiling


class ProjectionEngine:
    """Computes the capability table (set of handles) for an agent given
    its profile, optional task context, and the currently available
    capabilities.

    Rules are stored as a mapping from capability name to
    :class:`CapabilityRule`.  When no explicit rule exists for a capability,
    a level-based default is applied.

    Additional projection factors (per README Section 6.2):
    - Long-term permission overrides (per-agent, per-capability ceilings)
    - Elevation engine write-back capability ceilings
    - Temporary tightening (system-wide strength clamp)
    """

    def __init__(self) -> None:
        self.capability_rules: dict[str, CapabilityRule] = {}
        # Per-agent long-term permission overrides: {agent_id: {cap_name: CapabilityStrength}}
        self._long_term_permissions: dict[str, dict[str, CapabilityStrength]] = {}
        # Elevation engine write-back ceilings: {agent_id: CapabilityStrength}
        self._elevation_ceilings: dict[str, CapabilityStrength] = {}
        # Temporary tightening: system-wide max strength clamp (None = no tightening)
        self._temporary_strength_ceiling: CapabilityStrength | None = None
        # Capabilities under temporary tightening
        self._tightened_capabilities: set[str] | None = None

    # ------------------------------------------------------------------
    # Rule management
    # ------------------------------------------------------------------

    def add_rule(self, capability_name: str, rule: CapabilityRule) -> None:
        """Add or update a capability rule."""
        self.capability_rules[capability_name] = rule

    def remove_rule(self, capability_name: str) -> None:
        """Remove a capability rule.  Silently ignores missing keys."""
        self.capability_rules.pop(capability_name, None)

    # ------------------------------------------------------------------
    # Long-term permission state
    # ------------------------------------------------------------------

    def set_long_term_permission(
        self, agent_id: str, capability_name: str, max_strength: CapabilityStrength,
    ) -> None:
        """Set a long-term permission override for a specific agent + capability."""
        if agent_id not in self._long_term_permissions:
            self._long_term_permissions[agent_id] = {}
        self._long_term_permissions[agent_id][capability_name] = max_strength

    def remove_long_term_permission(self, agent_id: str, capability_name: str) -> None:
        """Remove a long-term permission override."""
        if agent_id in self._long_term_permissions:
            self._long_term_permissions[agent_id].pop(capability_name, None)

    # ------------------------------------------------------------------
    # Elevation engine write-back ceiling
    # ------------------------------------------------------------------

    def set_elevation_ceiling(self, agent_id: str, ceiling: CapabilityStrength) -> None:
        """Set the capability ceiling written back by the Elevation Engine."""
        self._elevation_ceilings[agent_id] = ceiling

    def remove_elevation_ceiling(self, agent_id: str) -> None:
        """Remove the elevation ceiling for an agent."""
        self._elevation_ceilings.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Temporary tightening
    # ------------------------------------------------------------------

    def set_temporary_tightening(
        self,
        ceiling: CapabilityStrength,
        capabilities: set[str] | None = None,
    ) -> None:
        """Apply system-wide temporary tightening.

        Parameters
        ----------
        ceiling:
            The maximum strength allowed during tightening.
        capabilities:
            If provided, only these capabilities are tightened.
            If None, all capabilities are tightened.
        """
        self._temporary_strength_ceiling = ceiling
        self._tightened_capabilities = capabilities

    def clear_temporary_tightening(self) -> None:
        """Remove temporary tightening."""
        self._temporary_strength_ceiling = None
        self._tightened_capabilities = None

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------

    def project(
        self,
        agent: AgentProfile,
        task_state: TaskState | None,
        available_capabilities: list[str],
    ) -> list[CapabilityHandle]:
        """Compute the agent's capability table.

        For each capability in *available_capabilities*:

        1. If an explicit rule exists, check that the agent's level meets
           ``required_level``, the agent's role is in ``required_roles``, and
           (when a task is provided) the task stage is in ``task_stages``.
           The allowed strength is the minimum of the rule's
           ``max_strength`` and what the agent level permits.

        2. If no rule exists, default based on agent level:
           - NOVICE  -> READ_ONLY
           - JUNIOR+ -> PROPOSE
           - INTERMEDIATE+ -> APPLY_SCOPED
           - SENIOR+ -> APPLY_FULL

        3. Additional projection factors applied on top:
           - Long-term permission overrides clamp per-capability strength.
           - Elevation engine write-back ceiling clamps all capabilities.
           - Temporary tightening further reduces strength if active.

        Parameters
        ----------
        agent:
            The agent profile to project for.
        task_state:
            Optional task state used for stage-based gating.
        available_capabilities:
            The universe of capabilities to consider.

        Returns
        -------
        list[CapabilityHandle]
            Ordered list of handles the agent is permitted to use.
        """
        handles: list[CapabilityHandle] = []
        level_ceiling = _level_permitted_strength(agent.level)

        # Elevation engine write-back ceiling for this agent
        elevation_ceiling = self._elevation_ceilings.get(agent.agent_id)

        # Long-term permission overrides for this agent
        lt_perms = self._long_term_permissions.get(agent.agent_id, {})

        for cap_name in available_capabilities:
            rule = self.capability_rules.get(cap_name)

            if rule is not None:
                # Rule-based gating -------------------------------------------
                # Level check
                if agent.level < rule.required_level:
                    continue

                # Role check
                if agent.role not in rule.required_roles:
                    continue

                # Task-stage check (only when a task context is provided)
                if task_state is not None:
                    if task_state.stage not in rule.task_stages:
                        continue

                # Compute effective strength
                effective = _clamp_strength(
                    _min_strength(rule.max_strength, level_ceiling),
                    rule.max_strength,
                )
                # If allowed_strengths is non-empty, keep only strengths in that list
                # that are <= effective.
                if rule.allowed_strengths:
                    candidates = sorted(
                        rule.allowed_strengths, key=_strength_rank, reverse=True
                    )
                    picked: CapabilityStrength | None = None
                    for candidate in candidates:
                        if _strength_rank(candidate) <= _strength_rank(effective):
                            picked = candidate
                            break
                    if picked is None:
                        continue
                    effective = picked
            else:
                # Default level-based access ----------------------------------
                effective = level_ceiling

            # --- Apply additional projection factors (README Section 6.2) ---

            # Long-term permission override
            if cap_name in lt_perms:
                effective = _min_strength(effective, lt_perms[cap_name])

            # Elevation engine write-back ceiling
            if elevation_ceiling is not None:
                effective = _min_strength(effective, elevation_ceiling)

            # Temporary tightening
            if self._temporary_strength_ceiling is not None:
                if self._tightened_capabilities is None or cap_name in self._tightened_capabilities:
                    effective = _min_strength(effective, self._temporary_strength_ceiling)

            # Build constraints dict for transparency
            constraints: dict[str, Any] = {}
            if cap_name in lt_perms:
                constraints["long_term_override"] = lt_perms[cap_name].value
            if elevation_ceiling is not None:
                constraints["elevation_ceiling"] = elevation_ceiling.value
            if self._temporary_strength_ceiling is not None:
                if self._tightened_capabilities is None or cap_name in self._tightened_capabilities:
                    constraints["temporary_tightening"] = self._temporary_strength_ceiling.value

            handles.append(
                CapabilityHandle(
                    name=cap_name,
                    strength=effective,
                    constraints=constraints,
                    description=f"Projected access for {cap_name}",
                )
            )

        return handles

    # ------------------------------------------------------------------
    # Cache-key helper
    # ------------------------------------------------------------------

    @staticmethod
    def compute_context_hash(
        agent_id: str,
        task_id: str | None,
        level: int,
        role: str,
    ) -> str:
        """Deterministic hash for cache invalidation.

        The hash is derived from the four inputs that meaningfully
        affect the projection outcome.
        """
        raw = f"{agent_id}|{task_id}|{level}|{role}"
        return hashlib.sha256(raw.encode()).hexdigest()
