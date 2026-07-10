"""
tests/orchestration/test_critic_loop.py
---------------------------------------
The autonomous critic loop: validation, verbatim error injection, RETRY
re-dispatch, retry exhaustion, and escalation to the orchestrator.
"""
import json
import time

from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.bus import CRITIC_QUEUE, RESULTS_QUEUE, InMemoryBus, task_queue
from mythos.orchestration.config import OrchestrationConfig
from mythos.orchestration.critic import CriticAgent
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.schemas import (
    StateUpdate,
    TaskParameters,
    TaskPayload,
    UpdateStatus,
)

from .conftest import make_agent_config, make_payload


def make_update(payload, status=UpdateStatus.SUCCESS, attempt=1, error_log=None):
    return StateUpdate(
        trace_id=payload.trace_id,
        task_id=payload.task_id,
        agent_role="backend_dev",
        status=status.value,
        result_pointers=[],
        summary="worker summary",
        error_log=error_log,
        attempt=attempt,
        task_payload=json.loads(payload.to_json()),
    )


def make_critic(bus, verdicts=None, matrix=None, max_attempts=3):
    """A critic whose LLM judgment finishes with the next scripted verdict."""
    verdicts = list(verdicts or [])

    def factory():
        conclusion = verdicts.pop(0) if verdicts else "VERDICT: PASS"
        return StubLLM([
            LLMResponse(content=None, tool_name="finish",
                        tool_args={"conclusion": conclusion}),
        ])

    return CriticAgent(
        bus=bus,
        matrix=matrix or InMemoryDataMatrix(HashEmbedder()),
        config=OrchestrationConfig(max_attempts=max_attempts),
        agent_config=make_agent_config(),
        llm_factory=factory,
    )


def drain(bus, queue_name):
    """Non-blocking read of everything currently on an InMemoryBus queue."""
    out = []
    q = bus._get(queue_name)
    while not q.empty():
        out.append(q.get_nowait()[0])
    return out


class TestJudgmentValidation:
    def test_pass_verdict_publishes_validated(self):
        bus = InMemoryBus()
        critic = make_critic(bus, verdicts=["VERDICT: PASS"])
        critic.review(make_update(make_payload()))

        [body] = drain(bus, RESULTS_QUEUE)
        result = StateUpdate.from_json(body)
        assert result.status == UpdateStatus.VALIDATED.value
        assert result.agent_role == "critic"
        assert drain(bus, task_queue("backend_dev")) == []

    def test_fail_verdict_triggers_retry_with_verbatim_error(self):
        bus = InMemoryBus()
        verdict = "VERDICT: FAIL: NameError: name 'fib' is not defined"
        critic = make_critic(bus, verdicts=[verdict])
        critic.review(make_update(make_payload()))

        [body] = drain(bus, task_queue("backend_dev"))
        retry = TaskPayload.from_json(body)
        assert retry.system_instruction == "RETRY_SUBTASK"
        assert retry.attempt == 2
        assert retry.error_log == verdict          # verbatim, untouched
        assert retry.task_id == "task-1"           # same subtask identity
        assert drain(bus, RESULTS_QUEUE) == []     # orchestrator not involved

    def test_no_verdict_fails_safe(self):
        bus = InMemoryBus()
        critic = make_critic(bus, verdicts=["Everything looked plausible to me."])
        critic.review(make_update(make_payload()))
        assert drain(bus, task_queue("backend_dev"))  # retried, not validated

    def test_judgment_reads_artifact_from_matrix(self):
        from mythos.orchestration.schemas import MemoryNode

        bus = InMemoryBus()
        matrix = InMemoryDataMatrix(HashEmbedder())
        artifact = MemoryNode.create(
            node_type="artifact", content="ARTIFACT BODY", source="agent:backend_dev"
        )
        matrix.upsert(artifact)

        seen = []

        class RecordingStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                seen.append(json.dumps(messages))
                return LLMResponse(content=None, tool_name="finish",
                                   tool_args={"conclusion": "VERDICT: PASS"})

        critic = CriticAgent(
            bus=bus,
            matrix=matrix,
            config=OrchestrationConfig(),
            agent_config=make_agent_config(),
            llm_factory=RecordingStub,
        )
        update = make_update(make_payload())
        update.result_pointers = [artifact.node_id]
        critic.review(update)
        assert "ARTIFACT BODY" in seen[0]


class TestMechanicalValidation:
    def test_command_exit_zero_validates(self):
        bus = InMemoryBus()
        critic = make_critic(bus)
        payload = make_payload(
            task_parameters=TaskParameters(objective="x", validation_command="true")
        )
        critic.review(make_update(payload))
        [body] = drain(bus, RESULTS_QUEUE)
        assert StateUpdate.from_json(body).status == UpdateStatus.VALIDATED.value

    def test_command_failure_output_is_injected_verbatim(self):
        bus = InMemoryBus()
        critic = make_critic(bus)
        payload = make_payload(
            task_parameters=TaskParameters(
                objective="x",
                validation_command="echo 'assert failed: expected 55' >&2; exit 3",
            )
        )
        critic.review(make_update(payload))
        [body] = drain(bus, task_queue("backend_dev"))
        retry = TaskPayload.from_json(body)
        assert "assert failed: expected 55" in retry.error_log
        assert "[exit code 3]" in retry.error_log


class TestRetryExhaustion:
    def test_final_attempt_escalates_failure(self):
        bus = InMemoryBus()
        critic = make_critic(bus, verdicts=["VERDICT: FAIL: still broken"], max_attempts=3)
        critic.review(make_update(make_payload(), attempt=3))

        assert drain(bus, task_queue("backend_dev")) == []
        [body] = drain(bus, RESULTS_QUEUE)
        result = StateUpdate.from_json(body)
        assert result.status == UpdateStatus.FAILURE.value
        assert "still broken" in result.error_log

    def test_worker_failure_is_retried_without_validation(self):
        bus = InMemoryBus()
        critic = make_critic(bus, verdicts=[])  # judgment must never run
        trace = "Traceback (most recent call last):\nRuntimeError: boom"
        critic.review(
            make_update(make_payload(), status=UpdateStatus.FAILURE, error_log=trace)
        )
        [body] = drain(bus, task_queue("backend_dev"))
        retry = TaskPayload.from_json(body)
        assert retry.error_log == trace

    def test_update_without_payload_escalates(self):
        bus = InMemoryBus()
        critic = make_critic(bus)
        update = StateUpdate(
            trace_id="t", task_id="k", agent_role="backend_dev",
            status=UpdateStatus.FAILURE.value, error_log="boom",
        )
        critic.review(update)
        [body] = drain(bus, RESULTS_QUEUE)
        assert StateUpdate.from_json(body).status == UpdateStatus.FAILURE.value

    def test_success_without_payload_fails_closed(self):
        # A SUCCESS with no work order attached is unverifiable - it must
        # never be forwarded as VALIDATED.
        bus = InMemoryBus()
        critic = make_critic(bus)
        update = StateUpdate(
            trace_id="t", task_id="k", agent_role="backend_dev",
            status=UpdateStatus.SUCCESS.value, summary="trust me",
        )
        critic.review(update)
        [body] = drain(bus, RESULTS_QUEUE)
        result = StateUpdate.from_json(body)
        assert result.status == UpdateStatus.FAILURE.value
        assert "task_payload" in (result.error_log or "")


class TestCriticCrashConversion:
    def test_crash_in_review_publishes_structured_failure(self):
        bus = InMemoryBus()
        critic = make_critic(bus)

        def explode(update):
            raise RuntimeError("matrix exploded")

        critic.review = explode
        update = make_update(make_payload())
        critic._on_message(update.to_json())

        [body] = drain(bus, RESULTS_QUEUE)
        result = StateUpdate.from_json(body)
        assert result.status == UpdateStatus.FAILURE.value
        assert result.task_id == update.task_id
        assert "matrix exploded" in result.error_log
        assert "Traceback" in result.error_log


class TestStructuredVerdict:
    def test_submit_verdict_tool_is_authoritative(self):
        bus = InMemoryBus()

        def factory():
            return StubLLM([
                LLMResponse(content=None, tool_name="submit_verdict",
                            tool_args={"passed": False,
                                       "reason": "AssertionError: expected 55"}),
                LLMResponse(content=None, tool_name="finish",
                            tool_args={"conclusion": "done reviewing"}),
            ])

        critic = CriticAgent(
            bus=bus,
            matrix=InMemoryDataMatrix(HashEmbedder()),
            config=OrchestrationConfig(max_attempts=3),
            agent_config=make_agent_config(),
            llm_factory=factory,
        )
        critic.review(make_update(make_payload()))
        [body] = drain(bus, task_queue("backend_dev"))
        retry = TaskPayload.from_json(body)
        assert "AssertionError: expected 55" in retry.error_log


class TestFullLoopOverBus:
    def test_fail_then_pass_round_trip(self):
        """Worker fails validation once, retries, passes – all over the bus."""
        from mythos.orchestration.worker import WorkerAgent

        bus = InMemoryBus()
        matrix = InMemoryDataMatrix(HashEmbedder())

        # Worker: first run produces a bad result, second a good one.
        worker_runs = [
            [LLMResponse(content=None, tool_name="finish",
                         tool_args={"conclusion": "buggy output"})],
            [LLMResponse(content=None, tool_name="finish",
                         tool_args={"conclusion": "fixed output"})],
        ]

        def worker_factory():
            return StubLLM(worker_runs.pop(0))

        worker = WorkerAgent(
            role="backend_dev",
            bus=bus,
            matrix=matrix,
            config=OrchestrationConfig(),
            agent_config=make_agent_config(),
            llm_factory=worker_factory,
        )

        # Critic: fails the first result, passes the second.
        critic = make_critic(
            bus,
            verdicts=["VERDICT: FAIL: output is buggy", "VERDICT: PASS"],
            matrix=matrix,
        )

        worker.start()
        critic.start()
        try:
            bus.publish(worker.queue, make_payload().to_json())
            deadline = time.monotonic() + 10
            result = None
            results_q = bus._get(RESULTS_QUEUE)
            while time.monotonic() < deadline:
                try:
                    body, _ = results_q.get(timeout=0.1)
                    result = StateUpdate.from_json(body)
                    break
                except Exception:  # noqa: BLE001 – queue.Empty
                    continue
        finally:
            worker.stop()
            critic.stop()

        assert result is not None, "no result reached the orchestrator queue"
        assert result.status == UpdateStatus.VALIDATED.value
        assert worker_runs == []  # both scripted runs were consumed
