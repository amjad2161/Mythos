"""
mythos/orchestration/critic.py
------------------------------
CriticAgent – the swarm's adversarial quality gate.

The queue topology guarantees every worker result passes through the critic
before the orchestrator sees it.  For each incoming StateUpdate the critic:

* on worker ``SUCCESS``  – validates the result: first mechanically (the
  payload's ``validation_command``, exit code 0 = pass), otherwise by an LLM
  judgment run with a read/execute-only Tools API (the critic verifies, it
  never fixes).  A missing work order fails closed: an unverifiable result
  never validates.
* on validation failure – injects the exact failure output (verbatim) into
  the payload's ``error_log``, bumps ``attempt``, and re-publishes the task
  straight back to the worker's queue as ``RETRY_SUBTASK``.  The orchestrator
  and the user are not involved: the debug loop is autonomous.
* on pass / retries exhausted – publishes ``VALIDATED`` / ``FAILURE`` to
  ``q.orchestrator.results``; only then does the result bubble up.

A crash anywhere in review becomes a structured ``FAILURE`` on the results
queue (mirroring the worker's crash conversion) so the orchestrator is never
left waiting on a silently swallowed result.
"""
from __future__ import annotations

import dataclasses
import threading
import traceback
from typing import Callable, List, Optional, Tuple

from ..agent import MythosAgent
from ..config import MythosConfig
from ..llm import BaseLLM, create_llm
from ..tools import Tool, run_shell_command
from .bus import CRITIC_QUEUE, RESULTS_QUEUE, MessageBus, task_queue
from .config import OrchestrationConfig
from .matrix import DataMatrix
from .personas import Persona
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

When you are certain, call the `submit_verdict` tool with your verdict and
the exact reason (include verbatim error output on failure), then call
`finish` with a conclusion that starts with exactly "VERDICT: PASS" or
"VERDICT: FAIL: <exact reason>".
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
        persona: Optional["Persona"] = None,
    ) -> None:
        self._bus = bus
        self._matrix = matrix
        self._config = config
        self._agent_config = agent_config or MythosConfig.from_env()
        if persona is not None:
            self._agent_config = dataclasses.replace(
                self._agent_config,
                system_suffix=persona.compile_system_suffix(),
            )
        self._llm_factory = llm_factory
        # Production path: one SDK client reused across judgment runs (client
        # construction + connection pooling is not free).  Test path: the
        # factory is called per judgment for scripted StubLLMs.
        self._shared_llm: Optional[BaseLLM] = None
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
    # Review logic
    # ------------------------------------------------------------------

    def _on_message(self, body: str) -> None:
        update = StateUpdate.from_json(body)  # SchemaError -> bus redelivery path
        try:
            self.review(update)
        except Exception:  # noqa: BLE001 – a critic crash must not swallow the result
            self._bus.publish(
                RESULTS_QUEUE,
                StateUpdate(
                    trace_id=update.trace_id,
                    task_id=update.task_id,
                    agent_role=self.role,
                    status=UpdateStatus.FAILURE.value,
                    result_pointers=update.result_pointers,
                    summary="Critic crashed while reviewing the result.",
                    error_log=traceback.format_exc(),
                    attempt=update.attempt,
                ).to_json(),
            )

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
            # Fail closed: with no work order there is no objective to verify
            # against, and an unverifiable result must never validate.
            return False, "Missing task_payload; cannot validate the result."
        command = payload.task_parameters.validation_command.strip()
        if command:
            return self._validate_mechanically(command)
        return self._validate_by_judgment(update, payload)

    @staticmethod
    def _validate_mechanically(command: str) -> Tuple[bool, str]:
        """Deterministic check: run the command; exit code 0 = pass."""
        returncode, output = run_shell_command(command, timeout=_VALIDATION_TIMEOUT_S)
        if returncode == 0:
            return True, ""
        if output.startswith("ERROR:"):
            return False, f"{output}: $ {command}"
        return False, f"[exit code {returncode}] $ {command}\n{output}"

    def _validate_by_judgment(
        self, update: StateUpdate, payload: TaskPayload
    ) -> Tuple[bool, str]:
        """LLM judgment with a read/execute-only registry + structured verdict tool."""
        result_text = update.summary
        artifacts = self._matrix.get(update.result_pointers)
        if artifacts:
            result_text = "\n---\n".join(node.content for node in artifacts)

        prompt = _JUDGMENT_PROMPT.format(
            objective=payload.task_parameters.objective,
            criteria=payload.task_parameters.success_criteria or "(none stated)",
            result=result_text,
        )

        # Structured verdict channel: the tool call is authoritative; the
        # conclusion-prefix protocol remains as a fallback for models that
        # skip the tool.
        captured: List[Tuple[bool, str]] = []

        def submit_verdict(passed: bool, reason: str = "") -> str:
            captured.append((bool(passed), str(reason)))
            return "Verdict recorded. Now call `finish` with the same verdict."

        registry = build_registry_for_role(self.role)
        registry.register(Tool(
            name="submit_verdict",
            description=(
                "Record your final verdict on the subtask. Call exactly once "
                "when certain, before finishing."
            ),
            parameters={
                "passed": {"type": "boolean", "description": "True if the result satisfies the objective."},
                "reason": {"type": "string", "description": "Exact reason; include verbatim error output on failure."},
            },
            func=submit_verdict,
            required=["passed"],
        ))

        agent = MythosAgent(
            config=self._agent_config,
            llm=self._judgment_llm(),
            registry=registry,
        )
        conclusion = agent.run(prompt).strip()

        if captured:
            # Last-wins (LIFO): submit_verdict is meant to be called once, but
            # if the model calls it twice the most recent verdict is honored.
            passed, reason = captured[-1]
            return (True, "") if passed else (False, reason or conclusion)

        upper = conclusion.upper()
        if upper.startswith(_PASS_MARKER):
            return True, ""
        if upper.startswith(_FAIL_MARKER):
            return False, conclusion
        # No verdict at all (e.g. the judgment run hit an iteration cap):
        # fail safe - an unverifiable result must not validate.
        return False, f"Critic returned no explicit verdict: {conclusion}"

    def _judgment_llm(self) -> BaseLLM:
        if self._llm_factory is not None:
            return self._llm_factory()
        if self._shared_llm is None:
            self._shared_llm = create_llm(
                provider=self._agent_config.llm_provider,
                model=self._agent_config.llm_model,
                api_key=self._agent_config.llm_api_key,
            )
        return self._shared_llm

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
            retry = dataclasses.replace(
                payload,
                system_instruction=SystemInstruction.RETRY_SUBTASK.value,
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
