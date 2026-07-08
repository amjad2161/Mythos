"""
mythos/monitor.py
-----------------
Self-monitoring and reflection module for the Mythos autonomous agent.

The monitor tracks agent performance, detects anomalies (repeated failures,
looping behaviour, token budget exhaustion) and triggers corrective actions.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

@dataclass
class AgentEvent:
    """A single timestamped event in the agent's lifecycle."""
    timestamp: float
    event_type: str     # "iteration" | "tool_call" | "error" | "reflection" | "goal_complete"
    detail: str = ""

    def __str__(self) -> str:
        ts = time.strftime("%H:%M:%S", time.gmtime(self.timestamp))
        return f"[{ts}] {self.event_type}: {self.detail}"


# ---------------------------------------------------------------------------
# Health report
# ---------------------------------------------------------------------------

@dataclass
class HealthReport:
    """Snapshot of the agent's current health."""
    iteration: int
    consecutive_failures: int
    total_tool_calls: int
    is_looping: bool
    needs_reflection: bool
    alert: Optional[str] = None

    @property
    def is_healthy(self) -> bool:
        return self.alert is None


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class Monitor:
    """
    Watches the agent loop and surfaces health signals.

    Responsibilities
    ----------------
    * Count iterations and detect runaway loops.
    * Track consecutive tool/LLM failures and trigger recovery.
    * Periodically suggest a self-reflection step.
    * Detect repetitive tool calls (looping on the same action).
    """

    def __init__(
        self,
        max_iterations: int = 50,
        max_consecutive_failures: int = 5,
        reflection_interval: int = 5,
        loop_window: int = 6,       # look-back window for repetition detection
    ) -> None:
        self._max_iterations = max_iterations
        self._max_consecutive_failures = max_consecutive_failures
        self._reflection_interval = reflection_interval
        self._loop_window = loop_window

        self._iteration: int = 0
        self._consecutive_failures: int = 0
        self._total_tool_calls: int = 0
        self._events: Deque[AgentEvent] = deque(maxlen=200)
        self._recent_tool_calls: Deque[str] = deque(maxlen=loop_window)

    # ------------------------------------------------------------------
    # Event recording
    # ------------------------------------------------------------------

    def record_iteration(self) -> None:
        self._iteration += 1
        self._log("iteration", f"#{self._iteration}")

    def record_tool_call(
        self, tool_name: str, success: bool, detail: str = "", signature: Optional[str] = None
    ) -> None:
        self._total_tool_calls += 1
        # Loop detection keys on the full call signature (name + arguments) so
        # legitimately repeating a tool with *different* arguments — e.g.
        # writing several files — is not mistaken for an infinite loop.
        self._recent_tool_calls.append(signature or tool_name)
        if success:
            self._consecutive_failures = 0
            self._log("tool_call", f"{tool_name} → OK  {detail}")
        else:
            self._consecutive_failures += 1
            self._log("error", f"{tool_name} FAILED: {detail}")

    def record_llm_error(self, detail: str = "") -> None:
        self._consecutive_failures += 1
        self._log("error", f"LLM error: {detail}")

    def record_reflection(self, detail: str = "") -> None:
        # NB: reflection is a checkpoint, not a recovery guarantee.  It does not
        # reset the failure counter — otherwise a failure streak that happens to
        # span a reflection interval could loop forever without ever tripping
        # the consecutive-failure alert.
        self._log("reflection", detail)

    def record_goal_complete(self, conclusion: str = "") -> None:
        self._log("goal_complete", conclusion)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health(self) -> HealthReport:
        """Return a current health report."""
        is_looping = self._detect_loop()
        needs_reflection = (
            self._iteration > 0
            and self._iteration % self._reflection_interval == 0
        )

        alert: Optional[str] = None
        if self._iteration >= self._max_iterations:
            alert = f"Maximum iteration limit ({self._max_iterations}) reached."
        elif self._consecutive_failures >= self._max_consecutive_failures:
            alert = (
                f"Agent has failed {self._consecutive_failures} times in a row. "
                "Triggering self-recovery."
            )
        elif is_looping:
            alert = "Repetitive tool call pattern detected – possible infinite loop."

        return HealthReport(
            iteration=self._iteration,
            consecutive_failures=self._consecutive_failures,
            total_tool_calls=self._total_tool_calls,
            is_looping=is_looping,
            needs_reflection=needs_reflection,
            alert=alert,
        )

    def reset_failures(self) -> None:
        self._consecutive_failures = 0

    def reset(self) -> None:
        """Clear all counters and history so the monitor can drive a fresh run."""
        self._iteration = 0
        self._consecutive_failures = 0
        self._total_tool_calls = 0
        self._events.clear()
        self._recent_tool_calls.clear()

    # ------------------------------------------------------------------
    # Event log access
    # ------------------------------------------------------------------

    def event_log(self) -> List[AgentEvent]:
        return list(self._events)

    def last_events(self, n: int = 10) -> List[AgentEvent]:
        events = list(self._events)
        return events[-n:]

    def stats(self) -> str:
        return (
            f"Iterations: {self._iteration} | "
            f"Tool calls: {self._total_tool_calls} | "
            f"Consecutive failures: {self._consecutive_failures}"
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _log(self, event_type: str, detail: str) -> None:
        self._events.append(AgentEvent(time.time(), event_type, detail))

    def _detect_loop(self) -> bool:
        """True when the last *loop_window* tool calls are all identical."""
        calls = list(self._recent_tool_calls)
        if len(calls) < self._loop_window:
            return False
        return len(set(calls)) == 1
