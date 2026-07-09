"""
mythos/orchestration/critic.py
------------------------------
CriticAgent – the swarm's adversarial quality gate.

The queue topology guarantees every worker result passes through the critic
before the orchestrator sees it.  For each incoming StateUpdate the critic:

* on worker ``SUCCESS``  – validates the result: first mechanically (the
  payload's ``validation_command``, exit code 0 = pass), otherwise by an LLM
  judgment run with a read/execute-only Tools API (the critic verifies, it
  never fixes).
* on validation failure – injects the exact failure output (verbatim) into
  the payload's ``error_log``, bumps ``attempt``, and re-publishes the task
  straight back to the worker's queue as ``RETRY_SUBTASK``.  The orchestrator
  and the user are not involved: the debug loop is autonomous.
* on pass / retries exhausted – publishes ``VALIDATED`` / ``FAILURE`` to
  ``q.orchestrator.results``; only then does the result bubble up.
"""
from __future__ import annotations

import subprocess
import threading
from typing import Callable, Optional, Tuple

from ..agent import MythosAgent
from ..config import MythosConfig
from ..llm import BaseLLM
from .bus import CRITIC_QUEUE, RESULTS_QUEUE, MessageBus, task_queue
from .config import OrchestrationConfig
from .matrix import DataMatrix
from .roles import build_registry_for_role
from .schemas import (
    StateUpdate,
    SystemInstruction,
    TaskPayload,
    UpdateStatus,
)

_VALIDATION_TIMEOUT_S = 120
_PASS_MARKER = "VERDICT: PASS"
_FAIL_MARKER = "VERDICT: FAIL"

_JUDGMENT_PROMPT = """\
You are a QA critic. Verify whether a completed subtask actually satisfies
its objective. Inspect files and run commands as needed, but do NOT modify
anything - you verify, you do not fix.

OBJECTIVE:
{objective}

SUCCESS CRITERIA:
{criteria}

WORKER'S REPORTED RESULT:
{result}

When you are certain, call `finish` with a conclusion that starts with
exactly "VERDICT: PASS" or "VERDICT: FAIL: <exact reason and error output>".
"""


class CriticAgent:
    """Intercepts every worker StateUpdate and drives the retry loop."""

    role = "critic"

    def __init__(
        self,
        bus: MessageBus,
        matrix: DataMatrix,
        config: OrchestrationConfig,
        agent_config: Optional[MythosConfig] = None,
        llm_factory: Optional[Callable[[], BaseLLM]] = None,
    ) -> None:
        self._bus = bus
        self._matrix = matrix
        self._config = config
        self._agent_config = agent_config or MythosConfig.from_env()
        self._llm_factory = llm_factory
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._bus.declare_queue(CRITIC_QUEUE)
        self._bus.declare_queue(RESULTS_QUEUE)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._bus.consume,
            args=(CRITIC_QUEUE, self._on_message, self._stop),
            name="critic",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # Review logic
    # ------------------------------------------------------------------

    def _on_message(self, body: str) -> None:
        update = StateUpdate.from_json(body)  # SchemaError -> bus redelivery path
        self.review(update)

    def review(self, update: StateUpdate) -> None:
        """Validate one worker StateUpdate and route the outcome."""
        payload = update.payload()

        if update.status == UpdateStatus.FAILURE.value:
            # The worker itself failed - its error_log is already verbatim.
            self._retry_or_escalate(update, payload, update.error_log or update.summary)
            return

        passed, failure_output = self._validate(update, payload)
        if passed:
            self._bus.publish(
                RESULTS_QUEUE,
                StateUpdate(
                    trace_id=update.trace_id,
                    task_id=update.task_id,
                    agent_role=self.role,
                    status=UpdateStatus.VALIDATED.value,
                    result_pointers=update.result_pointers,
                    summary=update.summary,
                    metrics=update.metrics,
                    attempt=update.attempt,
                ).to_json(),
            )
        else:
            self._retry_or_escalate(update, payload, failure_output)

    # ------------------------------------------------------------------
    # Validation strategies
    # ------------------------------------------------------------------

    def _validate(
        self, update: StateUpdate, payload: Optional[TaskPayload]
    ) -> Tuple[bool, str]:
        """Return (passed, verbatim failure output)."""
        if payload is None:
            # No work order attached: nothing to check against, accept as-is.
            return True, ""
        command = payload.task_parameters.validation_command.strip()
        if command:
            return self._validate_mechanically(command)
        return self._validate_by_judgment(update, payload)

    @staticmethod
    def _validate_mechanically(command: str) -> Tuple[bool, str]:
        """Deterministic check: run the command; exit code 0 = pass."""
        try:
            result = subprocess.run(
                command,
                shell=True,  # noqa: S602 – command comes from the trusted workflow definition
                capture_output=True,
                text=True,
                timeout=_VALIDATION_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return False, f"Validation command timed out after {_VALIDATION_TIMEOUT_S}s: {command}"
        if result.returncode == 0:
            return True, ""
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return False, f"[exit code {result.returncode}] $ {command}\n{output}"

    def _validate_by_judgment(
        self, update: StateUpdate, payload: TaskPayload
    ) -> Tuple[bool, str]:
        """LLM judgment with a read/execute-only registry."""
        result_text = update.summary
        artifacts = self._matrix.get(update.result_pointers)
        if artifacts:
            result_text = "\n---\n".join(node.content for node in artifacts)

        prompt = _JUDGMENT_PROMPT.format(
            objective=payload.task_parameters.objective,
            criteria=payload.task_parameters.success_criteria or "(none stated)",
            result=result_text,
        )
        agent = MythosAgent(
            config=self._agent_config,
            llm=self._llm_factory() if self._llm_factory else None,
            registry=build_registry_for_role(self.role),
        )
        conclusion = agent.run(prompt).strip()

        upper = conclusion.upper()
        if upper.startswith(_PASS_MARKER):
            return True, ""
        if upper.startswith(_FAIL_MARKER):
            return False, conclusion
        # No explicit verdict: fail safe - an unverifiable result must not
        # reach the orchestrator as validated.
        return False, f"Critic returned no explicit verdict: {conclusion}"

    # ------------------------------------------------------------------
    # Retry loop
    # ------------------------------------------------------------------

    def _retry_or_escalate(
        self,
        update: StateUpdate,
        payload: Optional[TaskPayload],
        failure_output: str,
    ) -> None:
        if payload is not None and update.attempt < self._config.max_attempts:
            retry = TaskPayload(
                system_instruction=SystemInstruction.RETRY_SUBTASK.value,
                trace_id=payload.trace_id,
                task_id=payload.task_id,
                orchestrator_node=payload.orchestrator_node,
                target_agent=payload.target_agent,
                task_parameters=payload.task_parameters,
                constraints=payload.constraints,
                callback_queue=payload.callback_queue,
                attempt=update.attempt + 1,
                error_log=failure_output,  # verbatim – the worker sees the exact trace
            )
            self._bus.publish(task_queue(payload.target_agent.role), retry.to_json())
            return

        self._bus.publish(
            RESULTS_QUEUE,
            StateUpdate(
                trace_id=update.trace_id,
                task_id=update.task_id,
                agent_role=self.role,
                status=UpdateStatus.FAILURE.value,
                result_pointers=update.result_pointers,
                summary=(
                    f"Subtask failed after {update.attempt} attempt(s); "
                    "retries exhausted."
                    if payload is not None
                    else "Subtask failed and no TaskPayload was attached to retry."
                ),
                error_log=failure_output,
                metrics=update.metrics,
                attempt=update.attempt,
            ).to_json(),
        )
