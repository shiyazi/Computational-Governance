"""Centralized Reputation Rating (中心化信誉评级).

This is the ONLY long-term agent profiling center.  It maintains
multi-dimensional performance profiles computed from observability logs
and task history, and serves the Elevation Engine and Discipline System.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from cgc.foundation.observability import ObservabilityLog
from cgc.foundation.task_state import TaskStateCore
from cgc.models import ReputationProfile


# ---------------------------------------------------------------------------
# Internal tracking records
# ---------------------------------------------------------------------------

class _TaskOutcome:
    __slots__ = ("task_id", "success", "first_pass", "timestamp")

    def __init__(self, task_id: str, success: bool, first_pass: bool, timestamp: float) -> None:
        self.task_id = task_id
        self.success = success
        self.first_pass = first_pass
        self.timestamp = timestamp


class _ReworkEvent:
    __slots__ = ("task_id", "timestamp")

    def __init__(self, task_id: str, timestamp: float) -> None:
        self.task_id = task_id
        self.timestamp = timestamp


class _BreakageEvent:
    __slots__ = ("task_id", "severity", "timestamp")

    def __init__(self, task_id: str, severity: float, timestamp: float) -> None:
        self.task_id = task_id
        self.severity = severity
        self.timestamp = timestamp


# ---------------------------------------------------------------------------
# Score history entry (used for stability computation)
# ---------------------------------------------------------------------------

class _ScoreSnapshot:
    __slots__ = ("score", "timestamp")

    def __init__(self, score: float, timestamp: float) -> None:
        self.score = score
        self.timestamp = timestamp


class _DifficultyRecord:
    """Tracks task difficulty for difficulty-correction in scoring (Section 16.3)."""
    __slots__ = ("task_id", "difficulty", "timestamp")

    def __init__(self, task_id: str, difficulty: float, timestamp: float) -> None:
        self.task_id = task_id
        self.difficulty = difficulty  # 0.0 (trivial) to 1.0 (extremely hard)
        self.timestamp = timestamp


# ---------------------------------------------------------------------------
# Weights for long_term_score computation
# ---------------------------------------------------------------------------

_LONG_TERM_WEIGHTS: dict[str, float] = {
    "completion_rate": 0.20,
    "first_pass_acceptance": 0.15,
    "rework_rate": -0.10,        # negative because lower is better
    "downstream_breakage": -0.10,
    "human_correction_rate": -0.10,
    "stability": 0.10,
    "role_fitness": 0.10,
    "delegation_quality": 0.05,
    "review_quality": 0.05,
    "risk_tendency": -0.05,
}


class ReputationRating:
    """In-memory store and evaluator for agent reputation profiles.

    This is the single long-term profiling center for the governance
    fabric.  Profiles are recomputed from raw event buffers combined
    with the shared :class:`TaskStateCore` and
    :class:`ObservabilityLog`.

    Parameters
    ----------
    task_core:
        The shared task-state store.
    observability:
        The append-only observability log for recording reputation events.
    """

    def __init__(
        self,
        task_core: TaskStateCore,
        observability: ObservabilityLog,
    ) -> None:
        self._task_core = task_core
        self._observability = observability

        self.profiles: dict[str, ReputationProfile] = {}
        self.time_window: float = 30 * 24 * 3600  # 30 days

        # Per-agent raw event buffers
        self._task_outcomes: dict[str, list[_TaskOutcome]] = {}
        self._rework_events: dict[str, list[_ReworkEvent]] = {}
        self._breakage_events: dict[str, list[_BreakageEvent]] = {}
        self._score_history: dict[str, list[_ScoreSnapshot]] = {}
        # Difficulty records for difficulty-correction (Section 16.3)
        self._difficulty_records: dict[str, list[_DifficultyRecord]] = {}

        # Discipline event counter (retained for backward compat)
        self._discipline_events: dict[str, int] = {}

        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Profile access
    # ------------------------------------------------------------------

    async def get_profile(self, agent_id: str) -> ReputationProfile:
        """Return the reputation profile for *agent_id*.

        If no profile exists yet, a fresh default profile is created and
        stored.
        """
        async with self._lock:
            if agent_id not in self.profiles:
                profile = ReputationProfile.create(agent_id=agent_id)
                self.profiles[agent_id] = profile
            return self.profiles[agent_id]

    # ------------------------------------------------------------------
    # Profile recomputation
    # ------------------------------------------------------------------

    async def update_profile(self, agent_id: str) -> ReputationProfile:
        """Recompute the profile for *agent_id* from observability logs
        and task history.

        Computes all multi-dimensional metrics as well as the composite
        ``long_term_score``, ``short_term_score``, and ``maturity_score``.
        """
        now = time.time()
        window_start = now - self.time_window
        short_term_start = now - 7 * 24 * 3600  # recent 7 days

        # ---- Gather task outcomes from internal buffer ----
        outcomes = self._task_outcomes.get(agent_id, [])
        window_outcomes = [o for o in outcomes if o.timestamp >= window_start]
        recent_outcomes = [o for o in outcomes if o.timestamp >= short_term_start]

        total_in_window = len(window_outcomes)
        total_recent = len(recent_outcomes)

        # completion_rate
        if total_in_window > 0:
            completed = sum(1 for o in window_outcomes if o.success)
            completion_rate = completed / total_in_window
        else:
            completion_rate = 0.0

        # first_pass_acceptance
        if total_in_window > 0:
            first_pass_count = sum(1 for o in window_outcomes if o.first_pass)
            first_pass_acceptance = first_pass_count / total_in_window
        else:
            first_pass_acceptance = 0.0

        # ---- Rework rate ----
        reworks = self._rework_events.get(agent_id, [])
        window_reworks = [r for r in reworks if r.timestamp >= window_start]
        if total_in_window > 0:
            rework_rate = len(window_reworks) / total_in_window
        else:
            rework_rate = 0.0

        # ---- Downstream breakage ----
        breakages = self._breakage_events.get(agent_id, [])
        window_breakages = [b for b in breakages if b.timestamp >= window_start]
        if total_in_window > 0:
            downstream_breakage = len(window_breakages) / total_in_window
        else:
            downstream_breakage = 0.0

        # Average breakage severity (0..1)
        breakage_severity = 0.0
        if window_breakages:
            breakage_severity = sum(b.severity for b in window_breakages) / len(
                window_breakages
            )

        # ---- Human correction rate (from observability) ----
        human_correction_rate = await self._compute_human_correction_rate(
            agent_id, window_start,
        )

        # ---- Role fitness, delegation quality, review quality ----
        role_fitness = await self._compute_role_fitness(agent_id, window_start)
        delegation_quality = await self._compute_delegation_quality(
            agent_id, window_start,
        )
        review_quality = await self._compute_review_quality(agent_id, window_start)

        # ---- Risk tendency ----
        risk_tendency = await self._compute_risk_tendency(
            agent_id, window_start, breakage_severity,
        )

        # ---- Stability (1.0 - variance of recent composite scores) ----
        snapshots = self._score_history.get(agent_id, [])
        window_snapshots = [s for s in snapshots if s.timestamp >= window_start]
        stability = self._compute_stability(window_snapshots)

        # ---- Composite scores ----
        # Difficulty correction factor (Section 16.3): agents who succeed on
        # harder tasks deserve more credit.  The correction boosts scores
        # proportionally to the average difficulty of successful tasks.
        difficulty_correction = self._compute_difficulty_correction(
            agent_id, window_start,
        )

        long_term_score = self._compute_long_term_score(
            completion_rate=completion_rate,
            first_pass_acceptance=first_pass_acceptance,
            rework_rate=rework_rate,
            downstream_breakage=downstream_breakage,
            human_correction_rate=human_correction_rate,
            stability=stability,
            role_fitness=role_fitness,
            delegation_quality=delegation_quality,
            review_quality=review_quality,
            risk_tendency=risk_tendency,
        )

        # short_term_score uses only the last 7 days of outcomes
        if total_recent > 0:
            recent_completed = sum(1 for o in recent_outcomes if o.success)
            recent_fp = sum(1 for o in recent_outcomes if o.first_pass)
            short_term_score = (recent_completed + recent_fp) / (2 * total_recent)
        else:
            short_term_score = long_term_score

        # maturity_score combines breadth of experience and consistency
        maturity_score = self._compute_maturity_score(
            total_tasks=total_in_window,
            completion_rate=completion_rate,
            stability=stability,
            time_active=self.time_window,
        )

        # Apply difficulty correction to composite scores (Section 16.3)
        long_term_score = min(1.0, long_term_score * (1.0 + difficulty_correction))
        maturity_score = min(1.0, maturity_score * (1.0 + difficulty_correction * 0.5))

        # Store a score snapshot for future stability calculations
        snapshot = _ScoreSnapshot(score=long_term_score, timestamp=now)
        async with self._lock:
            if agent_id not in self._score_history:
                self._score_history[agent_id] = []
            self._score_history[agent_id].append(snapshot)

        profile = ReputationProfile(
            agent_id=agent_id,
            completion_rate=completion_rate,
            first_pass_acceptance=first_pass_acceptance,
            rework_rate=rework_rate,
            downstream_breakage=downstream_breakage,
            human_correction_rate=human_correction_rate,
            stability=stability,
            role_fitness=role_fitness,
            delegation_quality=delegation_quality,
            review_quality=review_quality,
            risk_tendency=risk_tendency,
            long_term_score=long_term_score,
            short_term_score=short_term_score,
            maturity_score=maturity_score,
            last_updated=now,
        )

        async with self._lock:
            self.profiles[agent_id] = profile

        await self._observability.log(
            event_type="reputation_profile_updated",
            source="ReputationRating",
            agent_id=agent_id,
            details={
                "long_term_score": long_term_score,
                "short_term_score": short_term_score,
                "maturity_score": maturity_score,
            },
        )

        return profile

    # ------------------------------------------------------------------
    # Event recording
    # ------------------------------------------------------------------

    async def record_task_completion(
        self,
        agent_id: str,
        task_id: str,
        success: bool,
        first_pass: bool,
        difficulty: float | None = None,
    ) -> None:
        """Record the outcome of a task for *agent_id*.

        Parameters
        ----------
        difficulty:
            Optional difficulty score in [0, 1].  Used for difficulty
            correction in long-term scoring (Section 16.3).
        """
        now = time.time()
        outcome = _TaskOutcome(
            task_id=task_id, success=success, first_pass=first_pass, timestamp=now,
        )
        async with self._lock:
            self._task_outcomes.setdefault(agent_id, []).append(outcome)

        # Record difficulty if provided
        if difficulty is not None:
            difficulty = max(0.0, min(1.0, difficulty))
            rec = _DifficultyRecord(
                task_id=task_id, difficulty=difficulty, timestamp=now,
            )
            async with self._lock:
                self._difficulty_records.setdefault(agent_id, []).append(rec)

        await self._observability.log(
            event_type="task_completion_recorded",
            source="ReputationRating",
            agent_id=agent_id,
            task_id=task_id,
            details={
                "success": success,
                "first_pass": first_pass,
                "difficulty": difficulty,
            },
        )

    async def record_rework(self, agent_id: str, task_id: str) -> None:
        """Record a rework event for *agent_id* on *task_id*."""
        now = time.time()
        event = _ReworkEvent(task_id=task_id, timestamp=now)
        async with self._lock:
            self._rework_events.setdefault(agent_id, []).append(event)

        await self._observability.log(
            event_type="rework_recorded",
            source="ReputationRating",
            agent_id=agent_id,
            task_id=task_id,
        )

    async def record_downstream_breakage(
        self,
        agent_id: str,
        task_id: str,
        severity: float,
    ) -> None:
        """Record a downstream breakage event attributed to *agent_id*.

        Parameters
        ----------
        severity:
            Float in [0, 1] indicating how severe the breakage was.
        """
        severity = max(0.0, min(1.0, severity))
        now = time.time()
        event = _BreakageEvent(task_id=task_id, severity=severity, timestamp=now)
        async with self._lock:
            self._breakage_events.setdefault(agent_id, []).append(event)

        await self._observability.log(
            event_type="downstream_breakage_recorded",
            source="ReputationRating",
            agent_id=agent_id,
            task_id=task_id,
            details={"severity": severity},
        )

    # ------------------------------------------------------------------
    # Rankings and risk assessment
    # ------------------------------------------------------------------

    async def get_ranking(self, n: int = 10) -> list[tuple[str, float]]:
        """Return the top *n* agents ranked by ``long_term_score``.

        Returns a list of ``(agent_id, long_term_score)`` tuples sorted
        descending.
        """
        async with self._lock:
            items = [
                (aid, p.long_term_score)
                for aid, p in self.profiles.items()
            ]
        items.sort(key=lambda t: t[1], reverse=True)
        return items[:n]

    async def get_at_risk_agents(
        self,
        threshold: float = 0.5,
    ) -> list[tuple[str, ReputationProfile]]:
        """Return agents whose ``long_term_score`` is below *threshold*.

        Returns a list of ``(agent_id, profile)`` tuples sorted by score
        ascending (worst first).
        """
        async with self._lock:
            at_risk = [
                (aid, p)
                for aid, p in self.profiles.items()
                if p.long_term_score < threshold
            ]
        at_risk.sort(key=lambda t: t[1].long_term_score)
        return at_risk

    async def get_promotion_readiness(self, agent_id: str) -> dict[str, Any]:
        """Assess whether *agent_id* is ready for promotion.

        Returns a dict with:
        - ``ready`` (bool): whether all gates pass
        - ``score`` (float): the long-term score
        - ``gates`` (dict): individual gate checks
        - ``recommendation`` (str): human-readable summary
        """
        profile = await self.get_profile(agent_id)

        gates: dict[str, bool] = {
            "completion_rate": profile.completion_rate >= 0.8,
            "first_pass_acceptance": profile.first_pass_acceptance >= 0.7,
            "rework_rate": profile.rework_rate <= 0.2,
            "downstream_breakage": profile.downstream_breakage <= 0.1,
            "human_correction_rate": profile.human_correction_rate <= 0.15,
            "stability": profile.stability >= 0.6,
            "long_term_score": profile.long_term_score >= 0.7,
            "maturity_score": profile.maturity_score >= 0.5,
        }

        failed = [name for name, passed in gates.items() if not passed]
        all_passed = len(failed) == 0

        if all_passed:
            recommendation = (
                f"Agent {agent_id} meets all promotion criteria "
                f"(long_term_score={profile.long_term_score:.3f})."
            )
        else:
            recommendation = (
                f"Agent {agent_id} is not ready for promotion. "
                f"Failed gates: {', '.join(failed)}."
            )

        return {
            "ready": all_passed,
            "score": profile.long_term_score,
            "gates": gates,
            "recommendation": recommendation,
        }

    # ------------------------------------------------------------------
    # Discipline event compat (used by ElevationEngine)
    # ------------------------------------------------------------------

    async def record_discipline_event(self, agent_id: str) -> int:
        """Increment the discipline event counter for *agent_id*.

        Returns the new count.
        """
        async with self._lock:
            count = self._discipline_events.get(agent_id, 0) + 1
            self._discipline_events[agent_id] = count
            return count

    async def get_discipline_event_count(self, agent_id: str) -> int:
        """Return the number of discipline events recorded for *agent_id*."""
        async with self._lock:
            return self._discipline_events.get(agent_id, 0)

    async def clear_discipline_events(self, agent_id: str) -> None:
        """Reset the discipline event counter for *agent_id*."""
        async with self._lock:
            self._discipline_events.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Bulk access
    # ------------------------------------------------------------------

    async def list_profiles(self) -> list[ReputationProfile]:
        """Return all stored reputation profiles."""
        async with self._lock:
            return list(self.profiles.values())

    # ------------------------------------------------------------------
    # Internal computation helpers
    # ------------------------------------------------------------------

    async def _compute_human_correction_rate(
        self, agent_id: str, since: float,
    ) -> float:
        """Derive human correction rate from observability log entries."""
        logs = await self._observability.query(
            event_type="human_correction",
            agent_id=agent_id,
            since=since,
            limit=1_000_000,
        )
        total = await self._observability.query(
            agent_id=agent_id,
            since=since,
            limit=1_000_000,
        )
        if not total:
            return 0.0
        return len(logs) / len(total)

    async def _compute_role_fitness(
        self, agent_id: str, since: float,
    ) -> float:
        """Estimate role fitness from successful task completions matching
        agent's assigned role (proxied via task metadata)."""
        logs = await self._observability.query(
            event_type="role_fitness_assessment",
            agent_id=agent_id,
            since=since,
            limit=1_000_000,
        )
        if not logs:
            # Default to 0.5 (neutral) when no explicit assessments exist.
            return 0.5
        scores = [
            entry["details"].get("score", 0.5)
            for entry in logs
            if isinstance(entry.get("details"), dict)
        ]
        return sum(scores) / len(scores) if scores else 0.5

    async def _compute_delegation_quality(
        self, agent_id: str, since: float,
    ) -> float:
        """Estimate delegation quality from sub-task success rates."""
        delegated = await self._observability.query(
            event_type="delegation_outcome",
            agent_id=agent_id,
            since=since,
            limit=1_000_000,
        )
        if not delegated:
            return 0.5
        successes = sum(
            1
            for e in delegated
            if isinstance(e.get("details"), dict)
            and e["details"].get("success", False)
        )
        return successes / len(delegated)

    async def _compute_review_quality(
        self, agent_id: str, since: float,
    ) -> float:
        """Estimate review quality from review events."""
        reviews = await self._observability.query(
            event_type="review_completed",
            agent_id=agent_id,
            since=since,
            limit=1_000_000,
        )
        if not reviews:
            return 0.5
        qualities = [
            entry["details"].get("quality", 0.5)
            for entry in reviews
            if isinstance(entry.get("details"), dict)
        ]
        return sum(qualities) / len(qualities) if qualities else 0.5

    async def _compute_risk_tendency(
        self,
        agent_id: str,
        since: float,
        breakage_severity: float,
    ) -> float:
        """Compute risk tendency from discipline events, breakages, and
        observable risk indicators."""
        discipline_count = self._discipline_events.get(agent_id, 0)
        risk_events = await self._observability.query(
            event_type="risk_event",
            agent_id=agent_id,
            since=since,
            limit=1_000_000,
        )
        risk_density = len(risk_events) / 100.0  # normalise
        discipline_factor = min(discipline_count / 5.0, 1.0)
        return min(
            0.3 * risk_density + 0.4 * breakage_severity + 0.3 * discipline_factor,
            1.0,
        )

    @staticmethod
    def _compute_stability(snapshots: list[_ScoreSnapshot]) -> float:
        """Compute stability as ``1.0 - variance`` of recent scores.

        Returns a value clamped to [0, 1].
        """
        if len(snapshots) < 2:
            return 1.0
        scores = [s.score for s in snapshots]
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        return max(0.0, min(1.0, 1.0 - variance))

    @staticmethod
    def _compute_long_term_score(
        *,
        completion_rate: float,
        first_pass_acceptance: float,
        rework_rate: float,
        downstream_breakage: float,
        human_correction_rate: float,
        stability: float,
        role_fitness: float,
        delegation_quality: float,
        review_quality: float,
        risk_tendency: float,
    ) -> float:
        """Compute the weighted long-term composite score.

        Positive weights are added; negative weights are subtracted.
        The result is clamped to [0, 1].
        """
        raw = (
            _LONG_TERM_WEIGHTS["completion_rate"] * completion_rate
            + _LONG_TERM_WEIGHTS["first_pass_acceptance"] * first_pass_acceptance
            + _LONG_TERM_WEIGHTS["rework_rate"] * rework_rate  # negative weight
            + _LONG_TERM_WEIGHTS["downstream_breakage"] * downstream_breakage  # neg
            + _LONG_TERM_WEIGHTS["human_correction_rate"] * human_correction_rate  # neg
            + _LONG_TERM_WEIGHTS["stability"] * stability
            + _LONG_TERM_WEIGHTS["role_fitness"] * role_fitness
            + _LONG_TERM_WEIGHTS["delegation_quality"] * delegation_quality
            + _LONG_TERM_WEIGHTS["review_quality"] * review_quality
            + _LONG_TERM_WEIGHTS["risk_tendency"] * risk_tendency  # neg
        )
        # Positive contributions sum: 0.20+0.15+0.10+0.10+0.05+0.05 = 0.65
        # Negative contributions: rework, breakage, human_correction, risk
        # The max achievable raw score is ~0.65 (all positives at 1.0, negatives at 0.0).
        # Normalize to [0, 1] using 0.65 as the ceiling.
        return max(0.0, min(1.0, raw / 0.65)) if raw > 0 else max(0.0, raw)

    @staticmethod
    def _compute_maturity_score(
        *,
        total_tasks: int,
        completion_rate: float,
        stability: float,
        time_active: float,
    ) -> float:
        """Compute a maturity score combining experience breadth and
        consistency.

        Factors:
        - Experience breadth: ``min(1.0, total_tasks / 100)``
        - Reliability: ``completion_rate``
        - Consistency: ``stability``
        - Tenure: ``min(1.0, time_active / (90 * 86400))``
        """
        breadth = min(1.0, total_tasks / 100.0)
        tenure = min(1.0, time_active / (90.0 * 86400.0))
        raw = 0.3 * breadth + 0.3 * completion_rate + 0.25 * stability + 0.15 * tenure
        return max(0.0, min(1.0, raw))

    def _compute_difficulty_correction(
        self, agent_id: str, since: float,
    ) -> float:
        """Compute a difficulty correction factor (Section 16.3).

        Agents who succeed on harder tasks get a bonus.  The correction
        is the average difficulty of tasks in the window, scaled by the
        completion rate.  A higher average difficulty with good completion
        rate means a bigger bonus.

        Returns a multiplier in [0.0, 0.3] (at most 30% bonus).
        """
        records = self._difficulty_records.get(agent_id, [])
        window_records = [r for r in records if r.timestamp >= since]
        if not window_records:
            return 0.0

        avg_difficulty = sum(r.difficulty for r in window_records) / len(window_records)

        # Only get bonus for tasks that were actually completed
        outcomes = self._task_outcomes.get(agent_id, [])
        window_outcomes = [o for o in outcomes if o.timestamp >= since]
        if not window_outcomes:
            return 0.0
        completion_rate = sum(1 for o in window_outcomes if o.success) / len(window_outcomes)

        # Correction: avg_difficulty * completion_rate * 0.3
        # Max correction is 0.3 (30% bonus) when difficulty=1.0 and completion=1.0
        return avg_difficulty * completion_rate * 0.3
