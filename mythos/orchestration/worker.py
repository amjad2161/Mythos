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
from ..llm import BaseLLM, RetryingLLM, create_llm
from ..tools import _truncate
from .bus import CRITIC_QUEUE, MessageBus, task_queue
from .config import OrchestrationConfig
from .governor import CostGovernor
from .matrix import DataMatrix, fuse_context
from .personas import Persona
from .roles import build_registry_for_role
from .schemas import (
    MemoryNode,
    StateUpdate,
    SystemInstruction,
    TaskPayload,
    UpdateStatus,
)


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
        persona: Optional[Persona] = None,
        governor: Optional[CostGovernor] = None,
    ) -> None:
        self.role = role
        self.queue = task_queue(role)
        self._bus = bus
        self._matrix = matrix
        self._config = config
        self._persona = persona
        self._governor = governor
        # Template config for the inner MythosAgent; per-task constraints are
        # applied on a copy (dataclasses.replace) for every payload.
        self._agent_config = agent_config or MythosConfig.from_env()
        # Injection seam: tests supply a factory returning a scripted StubLLM
        # per task.  Production path: one SDK client built lazily and reused
        # across tasks (client construction + connection pools are not free).
        self._llm_factory = llm_factory
        self._shared_llm: Optional[BaseLLM] = None
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

    def request_stop(self) -> None:
        """Signal the consumer loop to stop (non-blocking)."""
        self._stop.set()

    def stop(self) -> None:
        self.request_stop()
        self.join()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
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

        # Cost governance: a tripped governor makes workers refuse NEW work
        # (in-flight tasks are unaffected) so a runaway goal can't keep
        # burning budget.
        if self._governor is not None:
            reason = self._governor.check()
            if reason is not None:
                return self._state_update(
                    payload,
                    status=UpdateStatus.FAILURE,
                    summary="Refused: cost governor tripped.",
                    error_log=reason,
                    wall_ms=0,
                )

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
        #    then graph traversal; results arrive trust-ranked.  Scoped to
        #    this payload's trace so stale runs in a persistent collection
        #    cannot leak in as high-trust context.
        nodes = self._matrix.navigate(
            need=params.objective,
            seed_ids=params.context_pointers,
            trace_id=payload.trace_id,
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
        registry = build_registry_for_role(
            self.role,
            constraints.forbidden_modules,
            access_level=payload.target_agent.access_level,
        )
        agent_config = dataclasses.replace(
            self._agent_config,
            # max_compute_tokens is enforced as a REAL cumulative token
            # budget by the Monitor; the configured max_iterations stays as
            # the independent runaway-loop bound.
            max_total_tokens=constraints.max_compute_tokens,
            max_wall_seconds=constraints.timeout_ms / 1000.0,
            system_suffix=(
                self._persona.compile_system_suffix()
                if self._persona
                else self._agent_config.system_suffix
            ),
        )
        agent = MythosAgent(
            config=agent_config,
            llm=self._task_llm(),
            registry=registry,
        )

        conclusion = agent.run(prompt)
        wall_ms = _elapsed_ms(started)
        failed = not agent.last_run_ok
        tokens = agent.monitor.token_usage
        if self._governor is not None:
            self._governor.record(agent.monitor.total_tokens)

        # 4. Persist the outcome as ground truth in the Data Matrix, linked
        #    back to the context it was produced from and tagged with the
        #    trace so later runs don't inherit it as context.
        artifact = MemoryNode.create(
            node_type="artifact" if not failed else "failure_report",
            content=conclusion,
            source=f"agent:{self.role}",
            edges=[
                {"relation": "produced_for", "target_id": pointer}
                for pointer in params.context_pointers
            ],
        )
        artifact.metadata["trace_id"] = payload.trace_id
        self._matrix.upsert(artifact)

        # A completed run that overshot timeout_ms is still reported (with a
        # flag) rather than failed – failing finished work would trigger a
        # destructive re-execution of its side effects.  Mid-run enforcement
        # is the Monitor's job.
        return self._state_update(
            payload,
            status=UpdateStatus.FAILURE if failed else UpdateStatus.SUCCESS,
            summary=_truncate(conclusion, 200),
            error_log=conclusion if failed else None,
            wall_ms=wall_ms,
            result_pointers=[artifact.node_id],
            deadline_exceeded=wall_ms > constraints.timeout_ms,
            tokens=tokens,
        )

    def _task_llm(self) -> BaseLLM:
        if self._llm_factory is not None:
            return self._llm_factory()
        if self._shared_llm is None:
            self._shared_llm = RetryingLLM(
                create_llm(
                    provider=self._agent_config.llm_provider,
                    model=self._agent_config.llm_model,
                    api_key=self._agent_config.llm_api_key,
                ),
                attempts=self._config.llm_retry_attempts,
                base_delay=self._config.llm_retry_base_s,
            )
        return self._shared_llm

    def _state_update(
        self,
        payload: TaskPayload,
        status: UpdateStatus,
        summary: str,
        wall_ms: int,
        error_log: Optional[str] = None,
        result_pointers: Optional[List[str]] = None,
        deadline_exceeded: bool = False,
        tokens: Optional[dict] = None,
    ) -> StateUpdate:
        metrics = {"wall_ms": wall_ms, "attempt": payload.attempt}
        if deadline_exceeded:
            metrics["deadline_exceeded"] = True
        if tokens:
            metrics["tokens"] = tokens
        return StateUpdate(
            trace_id=payload.trace_id,
            task_id=payload.task_id,
            agent_role=self.role,
            status=status.value,
            result_pointers=list(result_pointers or []),
            summary=summary,
            error_log=error_log,
            metrics=metrics,
            attempt=payload.attempt,
            task_payload=json.loads(payload.to_json()),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
