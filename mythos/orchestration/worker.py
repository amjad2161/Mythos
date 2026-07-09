"""
mythos/orchestration/worker.py
------------------------------
WorkerAgent – a specialised execution agent in the swarm.

A worker wraps the single-agent core (``MythosAgent`` + ``Executor``) and
gives it a message-bus lifecycle:

    TaskPayload (from q.tasks.<role>)
        → navigate the Data Matrix (context_pointers + semantic search)
        → fuse context into the objective prompt
        → MythosAgent.run() with the role's Tools API and payload constraints
        → write the produced artifact into the Data Matrix
        → StateUpdate (to the payload's callback queue – the critic)

Workers never emit free text onto the bus: a crash becomes a ``FAILURE``
StateUpdate carrying the verbatim traceback in ``error_log``.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
import traceback
from typing import Callable, List, Optional

from ..agent import MythosAgent
from ..config import MythosConfig
from ..llm import BaseLLM
from .bus import CRITIC_QUEUE, MessageBus, task_queue
from .config import OrchestrationConfig
from .matrix import DataMatrix, fuse_context
from .roles import build_registry_for_role
from .schemas import (
    MemoryNode,
    StateUpdate,
    SystemInstruction,
    TaskPayload,
    UpdateStatus,
)

# Conclusion prefixes MythosAgent.run uses to report abnormal termination
# (monitor stop, dependency deadlock, failed tasks).
_FAILURE_MARKERS = ("Agent stopped:", "Agent halted:", "Some tasks failed.")


class WorkerAgent:
    """A role-specialised swarm worker built on the single-agent core."""

    def __init__(
        self,
        role: str,
        bus: MessageBus,
        matrix: DataMatrix,
        config: OrchestrationConfig,
        agent_config: Optional[MythosConfig] = None,
        llm_factory: Optional[Callable[[], BaseLLM]] = None,
    ) -> None:
        self.role = role
        self.queue = task_queue(role)
        self._bus = bus
        self._matrix = matrix
        self._config = config
        # Template config for the inner MythosAgent; per-task constraints are
        # applied on a copy (dataclasses.replace) for every payload.
        self._agent_config = agent_config or MythosConfig.from_env()
        # Injection seam: tests supply a factory returning a scripted StubLLM
        # per task.  None -> MythosAgent builds the LLM from its config.
        self._llm_factory = llm_factory
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin consuming TaskPayloads on this worker's queue."""
        self._bus.declare_queue(self.queue)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._bus.consume,
            args=(self.queue, self._on_message, self._stop),
            name=f"worker-{self.role}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _on_message(self, body: str) -> None:
        payload = TaskPayload.from_json(body)  # SchemaError -> bus redelivery path
        update = self.handle(payload)
        self._bus.publish(payload.callback_queue or CRITIC_QUEUE, update.to_json())

    def handle(self, payload: TaskPayload) -> StateUpdate:
        """Execute one TaskPayload and return the structured result."""
        started = time.monotonic()
        try:
            return self._execute(payload, started)
        except Exception:  # noqa: BLE001 – a crash must become a structured FAILURE
            return self._state_update(
                payload,
                status=UpdateStatus.FAILURE,
                summary=f"{self.role} crashed while executing the subtask.",
                error_log=traceback.format_exc(),
                wall_ms=_elapsed_ms(started),
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute(self, payload: TaskPayload, started: float) -> StateUpdate:
        params = payload.task_parameters
        constraints = payload.constraints

        # 1. Autonomous navigation: explicit pointers + semantic search,
        #    then graph traversal; results arrive trust-ranked.
        nodes = self._matrix.navigate(
            need=params.objective,
            seed_ids=params.context_pointers,
        )
        context_block = fuse_context(nodes)

        # 2. Fuse the exact context window for the inner agent.
        prompt = params.objective
        if context_block:
            prompt = f"{params.objective}\n\n{context_block}"
        if (
            payload.system_instruction == SystemInstruction.RETRY_SUBTASK.value
            and payload.error_log
        ):
            prompt += (
                f"\n\nPREVIOUS ATTEMPT #{payload.attempt - 1} FAILED VALIDATION."
                "\nExact failure output (verbatim):\n"
                f"<<<ERROR LOG>>>\n{payload.error_log}\n<<<END ERROR LOG>>>\n"
                "Fix the problem and complete the objective."
            )

        # 3. Build the constrained inner agent: role Tools API minus
        #    forbidden modules, iteration cap derived from the token budget.
        registry = build_registry_for_role(self.role, constraints.forbidden_modules)
        agent_config = dataclasses.replace(
            self._agent_config,
            max_iterations=self._derive_iteration_cap(constraints.max_compute_tokens),
        )
        agent = MythosAgent(
            config=agent_config,
            llm=self._llm_factory() if self._llm_factory else None,
            registry=registry,
        )

        conclusion = agent.run(prompt)
        wall_ms = _elapsed_ms(started)

        # 4. Enforce the (cooperative) deadline: work that finished past the
        #    budget is reported as FAILURE so the critic/orchestrator can react.
        if wall_ms > constraints.timeout_ms:
            return self._state_update(
                payload,
                status=UpdateStatus.FAILURE,
                summary=f"{self.role} exceeded timeout_ms.",
                error_log=(
                    f"Subtask ran {wall_ms} ms, exceeding the "
                    f"{constraints.timeout_ms} ms constraint."
                ),
                wall_ms=wall_ms,
            )

        failed = conclusion.startswith(_FAILURE_MARKERS)

        # 5. Persist the outcome as ground truth in the Data Matrix, linked
        #    back to the context it was produced from.
        artifact = MemoryNode.create(
            node_type="artifact" if not failed else "failure_report",
            content=conclusion,
            source=f"agent:{self.role}",
            edges=[
                {"relation": "produced_for", "target_id": pointer}
                for pointer in params.context_pointers
            ],
        )
        self._matrix.upsert(artifact)

        return self._state_update(
            payload,
            status=UpdateStatus.FAILURE if failed else UpdateStatus.SUCCESS,
            summary=conclusion[:200],
            error_log=conclusion if failed else None,
            wall_ms=wall_ms,
            result_pointers=[artifact.node_id],
        )

    def _derive_iteration_cap(self, max_compute_tokens: int) -> int:
        """
        Approximate the payload's token budget as an iteration cap.

        The single-agent Monitor counts iterations, not tokens; one iteration
        can consume at most ``llm_max_tokens`` output tokens, so the budget
        divided by that is a safe upper bound (never above the configured
        agent cap, never below 1).
        """
        per_iteration = max(1, self._agent_config.llm_max_tokens)
        derived = max(1, max_compute_tokens // per_iteration)
        return min(derived, self._agent_config.max_iterations)

    def _state_update(
        self,
        payload: TaskPayload,
        status: UpdateStatus,
        summary: str,
        wall_ms: int,
        error_log: Optional[str] = None,
        result_pointers: Optional[List[str]] = None,
    ) -> StateUpdate:
        return StateUpdate(
            trace_id=payload.trace_id,
            task_id=payload.task_id,
            agent_role=self.role,
            status=status.value,
            result_pointers=list(result_pointers or []),
            summary=summary,
            error_log=error_log,
            metrics={"wall_ms": wall_ms, "attempt": payload.attempt},
            attempt=payload.attempt,
            task_payload=json.loads(payload.to_json()),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
