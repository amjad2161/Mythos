"""
mythos/orchestration/workflows.py
---------------------------------
Phase A rigid workflow definitions.

Phase A is deterministic automation: a workflow is a fixed, ordered list of
steps – step A necessarily leads to step B.  The orchestrator maps a workflow
onto a ``Plan`` (each step becomes a Task depending on the previous one) and
dispatches steps in order.

Critic review is deliberately NOT a workflow step: the queue topology forces
every worker result through the critic (see ``bus.py``), so review is
structural, not optional.

Phase B replaces these static definitions with LLM-driven decomposition in
the orchestrator; the ``Workflow`` shape is the seam where that lands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class WorkflowStep:
    """One rigid step: which role does what, and how the critic verifies it."""

    role: str
    # `{goal}` is substituted with the user's goal at dispatch time.
    objective_template: str
    # Deterministic critic check (shell command, exit 0 = pass); `{goal}` is
    # substituted.  Empty -> the critic falls back to LLM judgment.
    validation_command_template: str = ""
    success_criteria: str = ""
    forbidden_modules: List[str] = field(default_factory=list)

    def objective(self, goal: str) -> str:
        return self.objective_template.format(goal=goal)

    def validation_command(self, goal: str) -> str:
        return self.validation_command_template.format(goal=goal)


@dataclass
class Workflow:
    """A named, ordered, rigid sequence of steps."""

    name: str
    steps: List[WorkflowStep]


# ---------------------------------------------------------------------------
# Built-in workflows
# ---------------------------------------------------------------------------

# The default Phase A workflow: one dev step; validation is structural
# (worker -> critic -> orchestrator).
CODE_DELIVERY = Workflow(
    name="code_delivery",
    steps=[
        WorkflowStep(
            role="backend_dev",
            objective_template="Implement the following and verify it works: {goal}",
            success_criteria=(
                "The requested artifact exists and behaves as described in the goal."
            ),
        ),
    ],
)

BUILTIN_WORKFLOWS: Dict[str, Workflow] = {
    CODE_DELIVERY.name: CODE_DELIVERY,
}


def get_workflow(name: str) -> Workflow:
    workflow = BUILTIN_WORKFLOWS.get(name)
    if workflow is None:
        raise ValueError(
            f"Unknown workflow: '{name}'. Available: {sorted(BUILTIN_WORKFLOWS)}"
        )
    return workflow
