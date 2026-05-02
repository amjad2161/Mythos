"""
mythos/planner.py
-----------------
Goal and task planning for the Mythos autonomous agent.

The planner decomposes a high-level goal into an ordered list of sub-tasks,
tracks their status, and surfaces the next actionable task to the agent loop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, List, Optional


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Task data-type
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """A single atomic step toward achieving a goal."""
    id: int
    description: str
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None        # output/conclusion when completed
    error: Optional[str] = None         # error message when failed
    depends_on: List[int] = field(default_factory=list)  # IDs of prerequisite tasks

    def mark_done(self, result: str = "") -> None:
        self.status = TaskStatus.DONE
        self.result = result

    def mark_failed(self, error: str = "") -> None:
        self.status = TaskStatus.FAILED
        self.error = error

    def mark_skipped(self) -> None:
        self.status = TaskStatus.SKIPPED

    def is_ready(self, done_ids: set) -> bool:
        """Return True if all dependencies are satisfied."""
        return all(dep in done_ids for dep in self.depends_on)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "depends_on": self.depends_on,
        }


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class Plan:
    """
    An ordered collection of tasks that together achieve a goal.

    The plan is initially created with a single task derived from the raw
    goal string.  The agent can decompose it into sub-tasks at runtime.
    """

    def __init__(self, goal: str) -> None:
        self.goal = goal
        self._tasks: List[Task] = []
        self._next_id: int = 0

    # ------------------------------------------------------------------
    # Building the plan
    # ------------------------------------------------------------------

    def add_task(self, description: str, depends_on: Optional[List[int]] = None) -> Task:
        """Append a new task and return it."""
        task = Task(id=self._next_id, description=description, depends_on=depends_on or [])
        self._tasks.append(task)
        self._next_id += 1
        return task

    def load_from_list(self, descriptions: List[str]) -> None:
        """Replace current tasks with a fresh list (used after decomposition)."""
        self._tasks.clear()
        self._next_id = 0
        for desc in descriptions:
            self.add_task(desc)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def all_tasks(self) -> List[Task]:
        return list(self._tasks)

    def next_task(self) -> Optional[Task]:
        """Return the first PENDING task whose dependencies are met."""
        done_ids = {t.id for t in self._tasks if t.status == TaskStatus.DONE}
        for task in self._tasks:
            if task.status == TaskStatus.PENDING and task.is_ready(done_ids):
                return task
        return None

    def current_task(self) -> Optional[Task]:
        """Return the first IN_PROGRESS task, or the next pending one."""
        for task in self._tasks:
            if task.status == TaskStatus.IN_PROGRESS:
                return task
        return self.next_task()

    def is_complete(self) -> bool:
        """True when every task is DONE or SKIPPED (none pending/failed)."""
        return all(t.status in (TaskStatus.DONE, TaskStatus.SKIPPED) for t in self._tasks)

    def has_failures(self) -> bool:
        return any(t.status == TaskStatus.FAILED for t in self._tasks)

    def progress(self) -> str:
        """Human-readable progress summary."""
        total = len(self._tasks)
        done = sum(1 for t in self._tasks if t.status == TaskStatus.DONE)
        failed = sum(1 for t in self._tasks if t.status == TaskStatus.FAILED)
        pending = sum(1 for t in self._tasks if t.status == TaskStatus.PENDING)
        return f"{done}/{total} done, {failed} failed, {pending} pending"

    def __iter__(self) -> Iterator[Task]:
        return iter(self._tasks)

    def __len__(self) -> int:
        return len(self._tasks)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "progress": self.progress(),
            "tasks": [t.to_dict() for t in self._tasks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def summary(self) -> str:
        """One-line summary per task suitable for the system prompt."""
        lines = [f"GOAL: {self.goal}", "TASKS:"]
        for task in self._tasks:
            prefix = {
                TaskStatus.PENDING: "[ ]",
                TaskStatus.IN_PROGRESS: "[>]",
                TaskStatus.DONE: "[✓]",
                TaskStatus.FAILED: "[✗]",
                TaskStatus.SKIPPED: "[-]",
            }[task.status]
            lines.append(f"  {prefix} [{task.id}] {task.description}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class Planner:
    """
    Creates and manages Plans.

    The planner is intentionally thin – the heavy lifting (deciding *how*
    to decompose a goal) is done by the LLM inside the agent loop.  The
    planner is purely a data-management layer.
    """

    def __init__(self) -> None:
        self._plan: Optional[Plan] = None

    def new_plan(self, goal: str) -> Plan:
        """Create a fresh plan for *goal* with a single initial task."""
        plan = Plan(goal=goal)
        plan.add_task(description=goal)   # seed task; agent may decompose it
        self._plan = plan
        return plan

    def current_plan(self) -> Optional[Plan]:
        return self._plan

    def set_plan(self, plan: Plan) -> None:
        self._plan = plan
