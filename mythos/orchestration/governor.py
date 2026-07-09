"""
mythos/orchestration/governor.py
--------------------------------
CostGovernor – token-spend circuit breaker for the swarm.

Two independent budgets (0 = unlimited):

* **hourly** – a sliding 60-minute window over everything the swarm spends;
* **run**    – cumulative spend since the current goal started.

Workers consult ``check()`` *before* starting a task: a tripped governor makes
them refuse new work with a structured FAILURE (in-flight work is unaffected),
so a runaway goal cannot burn budget indefinitely.  Thread-safe – all workers
share one instance.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple

_WINDOW_S = 3600.0


class CostGovernor:
    """Sliding-window + per-run token budget breaker."""

    def __init__(self, hourly_token_budget: int = 0, run_token_budget: int = 0) -> None:
        self.hourly_token_budget = hourly_token_budget
        self.run_token_budget = run_token_budget
        self._events: Deque[Tuple[float, int]] = deque()
        self._window_total = 0
        self._run_total = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, tokens: int) -> None:
        """Account *tokens* of spend (call after each completed task)."""
        if tokens <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._events.append((now, tokens))
            self._window_total += tokens
            self._run_total += tokens
            self._prune(now)

    def reset_run(self) -> None:
        """Zero the per-run counter (call at the start of each goal)."""
        with self._lock:
            self._run_total = 0

    # ------------------------------------------------------------------
    # Checking
    # ------------------------------------------------------------------

    def check(self) -> Optional[str]:
        """Return a structured trip reason, or None when spending may continue."""
        with self._lock:
            self._prune(time.monotonic())
            if 0 < self.hourly_token_budget <= self._window_total:
                return (
                    "COST_GOVERNOR_TRIPPED: hourly token budget exhausted "
                    f"({self._window_total}/{self.hourly_token_budget})"
                )
            if 0 < self.run_token_budget <= self._run_total:
                return (
                    "COST_GOVERNOR_TRIPPED: run token budget exhausted "
                    f"({self._run_total}/{self.run_token_budget})"
                )
        return None

    @property
    def window_total(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return self._window_total

    @property
    def run_total(self) -> int:
        with self._lock:
            return self._run_total

    # ------------------------------------------------------------------
    # Internals (caller holds the lock)
    # ------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - _WINDOW_S
        while self._events and self._events[0][0] < cutoff:
            _, tokens = self._events.popleft()
            self._window_total -= tokens
