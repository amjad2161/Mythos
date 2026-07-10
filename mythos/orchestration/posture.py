"""
mythos/orchestration/posture.py
-------------------------------
Graded health posture — the swarm's multi-level kill-switch ladder.

Ported (trading-agnostic) from the tradingboy risk-posture pattern: instead of
a boolean "governor tripped / not tripped", the swarm derives a *graded*
posture from objective signals every time it is about to dispatch new work.
The posture never relaxes itself optimistically — the **most conservative
triggered condition wins** — so a degrading run throttles and then stops
cleanly rather than flipping on and off.

    NORMAL    full capacity, new work allowed
    REDUCED   new work allowed but throttled (lower concurrency / cheaper tier)
    PAUSED    no new work dispatched; in-flight work drains (soft budget / errors)
    HALT      hard stop — budget exhausted or a fatal condition

This complements :class:`~mythos.orchestration.governor.CostGovernor` (which
records spend and answers the boolean ``check()``) by turning the same signals
into a graduated control surface the orchestrator and control panel can act on.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Posture(IntEnum):
    NORMAL = 0
    REDUCED = 1
    PAUSED = 2
    HALT = 3

    @property
    def allows_new_work(self) -> bool:
        return self in (Posture.NORMAL, Posture.REDUCED)

    @property
    def concurrency_multiplier(self) -> float:
        """Fraction of normal parallelism this posture permits."""
        return {Posture.NORMAL: 1.0, Posture.REDUCED: 0.5}.get(self, 0.0)


@dataclass
class PostureInputs:
    """Objective signals the posture is derived from (all optional / defaulted)."""

    budget_exhausted: bool = False      # a hard token budget has been reached
    budget_fraction: float = 0.0        # 0..1 of the tightest budget consumed
    soft_budget_fraction: float = 0.8   # fraction at which we stop taking new work
    reduce_fraction: float = 0.6        # fraction at which we throttle
    error_burst: bool = False           # consecutive-failure storm
    consecutive_failures: int = 0
    max_consecutive_failures: int = 5
    backend_unhealthy: bool = False     # bus / matrix unreachable
    fatal: bool = False                 # an unrecoverable condition — stop the run


@dataclass
class PostureDecision:
    posture: Posture
    reason: str

    @property
    def allows_new_work(self) -> bool:
        return self.posture.allows_new_work

    @property
    def concurrency_multiplier(self) -> float:
        return self.posture.concurrency_multiplier


def evaluate_posture(inp: PostureInputs) -> PostureDecision:
    """Return the most conservative posture the inputs justify."""
    if inp.fatal:
        return PostureDecision(Posture.HALT, "fatal condition")
    if inp.budget_exhausted or inp.budget_fraction >= 1.0:
        return PostureDecision(Posture.HALT, "token budget exhausted")
    if inp.backend_unhealthy:
        return PostureDecision(Posture.PAUSED, "backend (bus/matrix) unhealthy")
    if inp.error_burst or (
        inp.max_consecutive_failures > 0
        and inp.consecutive_failures >= inp.max_consecutive_failures
    ):
        return PostureDecision(Posture.PAUSED, "consecutive-failure burst")
    if inp.budget_fraction >= inp.soft_budget_fraction:
        return PostureDecision(
            Posture.PAUSED,
            f"soft budget reached ({inp.budget_fraction:.0%})",
        )
    if inp.budget_fraction >= inp.reduce_fraction:
        return PostureDecision(
            Posture.REDUCED,
            f"budget {inp.budget_fraction:.0%} — throttling new work",
        )
    return PostureDecision(Posture.NORMAL, "ok")
