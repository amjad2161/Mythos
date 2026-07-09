"""
mythos/orchestration/orchestrator.py
------------------------------------
The Orchestrator ("Agent Boss") – layer 1 of the multi-agent architecture.

The orchestrator receives an abstract goal, decomposes it into a task matrix
(a ``Plan`` built from a rigid Phase A ``Workflow``), and routes each subtask
to a specialised worker as a strict ``TaskPayload``.  It never executes work
itself, and it only ever sees critic-validated results: workers report to the
critic, the critic reports to ``q.orchestrator.results``.

Phase B swaps the rigid workflow for LLM-driven decomposition; the dispatch /
collection loop below stays the same.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from typing import Dict, List, Optional

from ..planner import Plan, Task
from .bus import CRITIC_QUEUE, RESULTS_QUEUE, MessageBus, task_queue
from .config import OrchestrationConfig
from .matrix import DataMatrix
from .schemas import (
    Constraints,
    MemoryNode,
    StateUpdate,
    SystemInstruction,
    TargetAgent,
    TaskParameters,
    TaskPayload,
    UpdateStatus,
    TRUST_SYSTEM,
    TRUST_USER,
    new_task_id,
    new_trace_id,
)
from .workflows import Workflow, WorkflowStep

_SYSTEM_INSTRUCTION_TEXT = (
    "Ground rules for all agents: work only toward the stated objective, "
    "never fabricate data, files, or locations, and report results exactly "
    "as observed."
)


class SwarmTimeoutError(RuntimeError):
    """Raised when no validated result arrives within the configured window."""


class Orchestrator:
    """Decomposes goals, dispatches TaskPayloads, and collects validated results."""

    def __init__(
        self,
        bus: MessageBus,
        matrix: DataMatrix,
        config: OrchestrationConfig,
        workflow: Workflow,
    ) -> None:
        self._bus = bus
        self._matrix = matrix
        self._config = config
        self._workflow = workflow
        self._results: "queue.Queue[StateUpdate]" = queue.Queue()
        # Updates that arrived while we were waiting for a different task –
        # kept (not dropped) and checked before blocking on the queue.
        self._unmatched: Dict[str, StateUpdate] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin collecting critic-validated StateUpdates."""
        self._bus.declare_queue(RESULTS_QUEUE)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._bus.consume,
            args=(RESULTS_QUEUE, self._on_result, self._stop),
            name="orchestrator",
            daemon=True,
        )
        self._thread.start()

    def request_stop(self) -> None:
        """Signal the results consumer to stop (non-blocking)."""
        self._stop.set()

    def stop(self) -> None:
        self.request_stop()
        self.join()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _on_result(self, body: str) -> None:
        self._results.put(StateUpdate.from_json(body))

    # ------------------------------------------------------------------
    # Goal execution
    # ------------------------------------------------------------------

    def run(self, goal: str) -> str:
        """
        Drive *goal* through the workflow and return the final conclusion.

        Blocking: dispatches each ready subtask, waits for its validated (or
        terminally failed) StateUpdate, and advances the plan until complete.
        """
        trace_id = new_trace_id()

        # Seed the Data Matrix: the system instruction is absolute ground
        # truth (max trust, verbatim); the goal refines it.  The system node
        # id is derived from its content so repeated runs against the same
        # persistent collection re-use one node instead of accumulating
        # duplicates.
        system_node = MemoryNode.create(
            node_type="system_instruction",
            content=_SYSTEM_INSTRUCTION_TEXT,
            source="orchestrator",
            trust_score=TRUST_SYSTEM,
            verbatim_required=True,
        )
        system_node.node_id = str(
            uuid.uuid5(uuid.NAMESPACE_OID, _SYSTEM_INSTRUCTION_TEXT)
        )
        self._matrix.upsert(system_node)
        goal_node = MemoryNode.create(
            node_type="goal",
            content=goal,
            source="user",
            trust_score=TRUST_USER,
            verbatim_required=True,
            edges=[{"relation": "refines", "target_id": system_node.node_id}],
        )
        # Trace-tag the goal so later runs against the same collection don't
        # surface it as high-trust context for unrelated goals.
        goal_node.metadata["trace_id"] = trace_id
        self._matrix.upsert(goal_node)

        # Declare each role's queue once up front (not per dispatch).
        for role in {step.role for step in self._workflow.steps}:
            self._bus.declare_queue(task_queue(role))

        # Decompose: workflow -> Plan (each step depends on the previous).
        plan = Plan(goal=goal)
        steps_by_plan_id: Dict[int, WorkflowStep] = {}
        previous_id: Optional[int] = None
        for step in self._workflow.steps:
            task = plan.add_task(
                description=step.objective(goal),
                depends_on=[previous_id] if previous_id is not None else [],
            )
            steps_by_plan_id[task.id] = step
            previous_id = task.id

        # Dispatch loop: one ready subtask at a time (Phase A is strictly
        # sequential; concurrent dispatch is a Phase B concern).
        results: List[str] = []
        while not plan.is_complete():
            task = plan.next_task()
            if task is None:
                if plan.has_failures():
                    return "Goal failed. " + " | ".join(results or ["No validated results."])
                stuck = [t.description for t in plan.all_tasks() if t.status.value == "pending"]
                return (
                    "Orchestrator halted: unsatisfiable dependencies for "
                    f"{len(stuck)} task(s): " + "; ".join(stuck)
                )

            step = steps_by_plan_id[task.id]
            update = self._dispatch_and_wait(trace_id, goal, task, step, goal_node.node_id)

            if update.status == UpdateStatus.VALIDATED.value:
                summary = self._resolve_result(update)
                task.mark_done(summary)
                results.append(summary)
            else:
                task.mark_failed(update.error_log or update.summary)
                results.append(
                    f"[{step.role}] FAILED: {update.summary}"
                    + (f"\n{update.error_log}" if update.error_log else "")
                )

        if plan.has_failures():
            return "Goal failed. " + " | ".join(results)
        return " | ".join(results) if results else "Goal processing finished."

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _dispatch_and_wait(
        self,
        trace_id: str,
        goal: str,
        task: Task,
        step: WorkflowStep,
        goal_node_id: str,
    ) -> StateUpdate:
        payload = TaskPayload(
            system_instruction=SystemInstruction.EXECUTE_SUBTASK.value,
            trace_id=trace_id,
            task_id=new_task_id(),
            orchestrator_node=self._config.orchestrator_id,
            target_agent=TargetAgent(role=step.role),
            task_parameters=TaskParameters(
                objective=step.objective(goal),
                context_pointers=[goal_node_id],
                validation_command=step.validation_command(goal),
                success_criteria=step.success_criteria,
            ),
            constraints=Constraints(forbidden_modules=list(step.forbidden_modules)),
            callback_queue=CRITIC_QUEUE,
        )
        self._log(f"[Orchestrator] Dispatching task {payload.task_id} -> {step.role}")
        self._bus.publish(task_queue(step.role), payload.to_json())
        return self._wait_for(payload.task_id, payload.constraints)

    def _wait_for(self, task_id: str, constraints: Constraints) -> StateUpdate:
        """
        Block until the terminal StateUpdate for *task_id* arrives.

        The wait window covers the full retry budget the constraints permit
        (attempts x (execution + validation) plus slack), never less than the
        configured ``result_timeout_s``, and is enforced as an absolute
        deadline – a stream of unrelated updates cannot extend it.  Updates
        for other tasks are buffered, not dropped.
        """
        if self._config.result_timeout_s > 0:
            window_s = self._config.result_timeout_s
        else:
            # Auto (0): cover the full retry budget the constraints permit –
            # attempts x (execution deadline + validation/overhead slack).
            window_s = self._config.max_attempts * (
                constraints.timeout_ms / 1000.0 + 150.0
            )
        deadline = time.monotonic() + window_s
        while True:
            if task_id in self._unmatched:
                return self._unmatched.pop(task_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SwarmTimeoutError(
                    f"No validated result for task {task_id} within {window_s:.0f}s."
                )
            try:
                update = self._results.get(timeout=remaining)
            except queue.Empty:
                raise SwarmTimeoutError(
                    f"No validated result for task {task_id} within {window_s:.0f}s."
                ) from None
            if update.task_id == task_id:
                return update
            # An update for a different task (earlier goal, concurrent
            # dispatch in Phase B): keep it retrievable instead of dropping.
            self._unmatched[update.task_id] = update
            self._log(f"[Orchestrator] Buffered update for task {update.task_id}")

    def _resolve_result(self, update: StateUpdate) -> str:
        """Prefer the artifact content from the Data Matrix over the summary."""
        nodes = self._matrix.get(update.result_pointers)
        if nodes:
            return "\n".join(node.content for node in nodes)
        return update.summary or json.dumps(update.metrics)

    def _log(self, message: str) -> None:
        if self._config.verbose:
            print(message)
