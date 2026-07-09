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
from typing import TYPE_CHECKING, Dict, List, Optional

from ..planner import Plan, TaskStatus
from .bus import CRITIC_QUEUE, RESULTS_QUEUE, MessageBus, task_queue
from .config import OrchestrationConfig
from .ledger import TaskLedger
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

if TYPE_CHECKING:
    from .decomposer import DynamicDecomposer

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
        decomposer: Optional["DynamicDecomposer"] = None,
    ) -> None:
        self._bus = bus
        self._matrix = matrix
        self._config = config
        self._workflow = workflow
        # Phase B seam: when set, the goal is decomposed dynamically and
        # *workflow* serves only as the deterministic fallback.
        self._decomposer = decomposer
        self._ledger = TaskLedger(matrix)
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

        # Decompose: dynamically when a decomposer is wired (Phase B),
        # otherwise the rigid workflow.  Either way the result is a Workflow,
        # so everything downstream is identical.
        workflow = (
            self._decomposer.decompose(goal) if self._decomposer else self._workflow
        )

        # Declare each role's queue once up front (not per dispatch).
        for role in {step.role for step in workflow.steps}:
            self._bus.declare_queue(task_queue(role))

        # Workflow -> Plan.  A step's depends_on lists prerequisite step
        # indices; None means "the previous step" (sequential default), []
        # means independent - independent branches dispatch concurrently.
        plan = Plan(goal=goal)
        steps_by_plan_id: Dict[int, WorkflowStep] = {}
        index_by_plan_id: Dict[int, int] = {}
        plan_id_by_index: Dict[int, int] = {}
        for index, step in enumerate(workflow.steps):
            if step.depends_on is None:
                dep_ids = [plan_id_by_index[index - 1]] if index > 0 else []
            else:
                dep_ids = [plan_id_by_index[i] for i in step.depends_on]
            task = plan.add_task(
                description=step.objective(goal),
                depends_on=dep_ids,
            )
            steps_by_plan_id[task.id] = step
            index_by_plan_id[task.id] = index
            plan_id_by_index[index] = task.id

        # Durable, externalized progress: the ledger is the observable source
        # of truth for this goal's subtask states.
        ledger_id = self._ledger.create(
            trace_id=trace_id,
            goal=goal,
            steps=[
                {"role": step.role, "objective": step.objective(goal)}
                for step in workflow.steps
            ],
            goal_node_id=goal_node.node_id,
        )
        self._log(f"[Orchestrator] Ledger node: {ledger_id}")

        # Dispatch loop: every ready subtask is dispatched immediately, so
        # independent branches of the DAG execute concurrently; the loop then
        # waits for whichever in-flight subtask finishes first.
        tasks_by_plan_id = {t.id: t for t in plan.all_tasks()}
        results_by_index: Dict[int, str] = {}
        in_flight: Dict[str, int] = {}          # payload task_id -> plan task id
        constraints_in_flight: Dict[str, Constraints] = {}
        while True:
            while True:
                task = plan.next_task()
                if task is None:
                    break
                task.status = TaskStatus.IN_PROGRESS
                step = steps_by_plan_id[task.id]
                payload = self._dispatch(
                    trace_id, goal, step, goal_node.node_id,
                    ledger_id, index_by_plan_id[task.id],
                )
                in_flight[payload.task_id] = task.id
                constraints_in_flight[payload.task_id] = payload.constraints

            if not in_flight:
                break  # nothing running, nothing ready -> terminal state

            update = self._wait_for_any(
                set(in_flight), list(constraints_in_flight.values())
            )
            plan_task_id = in_flight.pop(update.task_id)
            constraints_in_flight.pop(update.task_id, None)
            task = tasks_by_plan_id[plan_task_id]
            step = steps_by_plan_id[plan_task_id]
            step_index = index_by_plan_id[plan_task_id]

            if update.status == UpdateStatus.VALIDATED.value:
                summary = self._resolve_result(update)
                task.mark_done(summary)
                results_by_index[step_index] = summary
                self._ledger.mark_terminal(
                    ledger_id, step_index, "validated", update.attempt, summary
                )
            else:
                task.mark_failed(update.error_log or update.summary)
                results_by_index[step_index] = (
                    f"[{step.role}] FAILED: {update.summary}"
                    + (f"\n{update.error_log}" if update.error_log else "")
                )
                self._ledger.mark_terminal(
                    ledger_id, step_index, "failed", update.attempt,
                    update.error_log or update.summary,
                )

        results = [results_by_index[i] for i in sorted(results_by_index)]
        if plan.has_failures():
            return "Goal failed. " + " | ".join(results or ["No validated results."])
        if plan.is_complete():
            return " | ".join(results) if results else "Goal processing finished."
        stuck = [t.description for t in plan.all_tasks() if t.status == TaskStatus.PENDING]
        return (
            "Orchestrator halted: unsatisfiable dependencies for "
            f"{len(stuck)} task(s): " + "; ".join(stuck)
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        trace_id: str,
        goal: str,
        step: WorkflowStep,
        goal_node_id: str,
        ledger_id: str,
        step_index: int,
    ) -> TaskPayload:
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
        self._ledger.mark_dispatched(ledger_id, step_index, payload.task_id)
        self._bus.publish(task_queue(step.role), payload.to_json())
        return payload

    def _wait_for_any(
        self, task_ids: "set[str]", constraints: List[Constraints]
    ) -> StateUpdate:
        """
        Block until the terminal StateUpdate for ANY of *task_ids* arrives.

        The wait window covers the largest retry budget the in-flight
        constraints permit (attempts x (execution + validation) plus slack),
        never less than the configured ``result_timeout_s``, and is enforced
        as an absolute deadline – a stream of unrelated updates cannot extend
        it.  Updates for other tasks are buffered, not dropped.
        """
        if self._config.result_timeout_s > 0:
            window_s = self._config.result_timeout_s
        else:
            # Auto (0): cover the full retry budget the constraints permit –
            # attempts x (execution deadline + validation/overhead slack).
            max_timeout_ms = max((c.timeout_ms for c in constraints), default=300_000)
            window_s = self._config.max_attempts * (max_timeout_ms / 1000.0 + 150.0)
        deadline = time.monotonic() + window_s
        while True:
            for task_id in task_ids:
                if task_id in self._unmatched:
                    return self._unmatched.pop(task_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SwarmTimeoutError(
                    f"No validated result for task(s) {sorted(task_ids)} "
                    f"within {window_s:.0f}s."
                )
            try:
                update = self._results.get(timeout=remaining)
            except queue.Empty:
                raise SwarmTimeoutError(
                    f"No validated result for task(s) {sorted(task_ids)} "
                    f"within {window_s:.0f}s."
                ) from None
            if update.task_id in task_ids:
                return update
            # An update for a task we're not awaiting (earlier goal / stale
            # run): keep it retrievable instead of dropping.
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
