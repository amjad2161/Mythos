"""
tests/orchestration/test_worker.py
----------------------------------
WorkerAgent behaviour driven by scripted StubLLMs over the in-memory drivers.
"""
import json

import pytest

from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.bus import InMemoryBus
from mythos.orchestration.config import OrchestrationConfig
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.roles import build_registry_for_role
from mythos.orchestration.schemas import (
    Constraints,
    MemoryNode,
    TaskParameters,
    UpdateStatus,
)
from mythos.orchestration.worker import WorkerAgent

from .conftest import make_agent_config, make_payload


def make_worker(responses, agent_config=None, matrix=None, bus=None):
    return WorkerAgent(
        role="backend_dev",
        bus=bus or InMemoryBus(),
        matrix=matrix or InMemoryDataMatrix(HashEmbedder()),
        config=OrchestrationConfig(bus_backend="inmemory", matrix_backend="inmemory"),
        agent_config=agent_config or make_agent_config(),
        llm_factory=lambda: StubLLM(list(responses)),
    )


class TestRoles:
    def test_backend_dev_has_write_tools(self):
        registry = build_registry_for_role("backend_dev")
        assert registry.get("write_file") is not None
        assert registry.get("finish") is not None

    def test_critic_is_read_and_execute_only(self):
        registry = build_registry_for_role("critic")
        assert registry.get("write_file") is None
        assert registry.get("append_file") is None
        assert registry.get("run_shell") is not None

    def test_forbidden_modules_are_stripped(self):
        registry = build_registry_for_role("backend_dev", ["run_shell", "write_file"])
        assert registry.get("run_shell") is None
        assert registry.get("write_file") is None
        assert registry.get("read_file") is not None

    def test_finish_cannot_be_forbidden(self):
        registry = build_registry_for_role("backend_dev", ["finish"])
        assert registry.get("finish") is not None

    def test_unknown_role_raises(self):
        with pytest.raises(ValueError):
            build_registry_for_role("astronaut")

    def test_role_listing_unknown_tool_raises(self):
        # A typo in a role allow-list must fail at startup, not produce a
        # silently under-tooled worker.
        from mythos.orchestration import roles

        original = roles.ROLE_TOOLS["backend_dev"]
        roles.ROLE_TOOLS["backend_dev"] = original + ["no_such_tool"]
        try:
            with pytest.raises(ValueError, match="no_such_tool"):
                build_registry_for_role("backend_dev")
        finally:
            roles.ROLE_TOOLS["backend_dev"] = original


class TestWorkerHandle:
    def test_success_produces_artifact_and_update(self):
        matrix = InMemoryDataMatrix(HashEmbedder())
        worker = make_worker(
            [LLMResponse(content=None, tool_name="finish",
                         tool_args={"conclusion": "Implemented the thing."})],
            matrix=matrix,
        )
        update = worker.handle(make_payload())

        assert update.status == UpdateStatus.SUCCESS.value
        assert update.agent_role == "backend_dev"
        assert update.task_id == "task-1"
        assert update.result_pointers
        [artifact] = matrix.get(update.result_pointers)
        assert artifact.node_type == "artifact"
        assert artifact.content == "Implemented the thing."
        # The originating payload rides along for the critic's retry loop.
        assert update.payload() == make_payload()

    def test_context_pointers_are_fused_into_prompt(self):
        matrix = InMemoryDataMatrix(HashEmbedder())
        spec = MemoryNode.create(
            node_type="system_instruction",
            content="THE SACRED SPEC",
            source="orchestrator",
            trust_score=1.0,
            verbatim_required=True,
        )
        matrix.upsert(spec)

        seen_prompts = []

        class RecordingStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                seen_prompts.append(json.dumps(messages))
                return LLMResponse(content=None, tool_name="finish",
                                   tool_args={"conclusion": "ok"})

        worker = WorkerAgent(
            role="backend_dev",
            bus=InMemoryBus(),
            matrix=matrix,
            config=OrchestrationConfig(),
            agent_config=make_agent_config(),
            llm_factory=RecordingStub,
        )
        payload = make_payload(
            task_parameters=TaskParameters(
                objective="Implement per the spec",
                context_pointers=[spec.node_id],
            )
        )
        worker.handle(payload)
        assert "THE SACRED SPEC" in seen_prompts[0]
        assert "<<<VERBATIM>>>" in seen_prompts[0]

    def test_retry_payload_injects_error_log_into_prompt(self):
        seen_prompts = []

        class RecordingStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                seen_prompts.append(json.dumps(messages))
                return LLMResponse(content=None, tool_name="finish",
                                   tool_args={"conclusion": "fixed"})

        worker = WorkerAgent(
            role="backend_dev",
            bus=InMemoryBus(),
            matrix=InMemoryDataMatrix(HashEmbedder()),
            config=OrchestrationConfig(),
            agent_config=make_agent_config(),
            llm_factory=RecordingStub,
        )
        payload = make_payload(
            system_instruction="RETRY_SUBTASK",
            attempt=2,
            error_log="SyntaxError: invalid syntax on line 3",
        )
        update = worker.handle(payload)
        assert update.status == UpdateStatus.SUCCESS.value
        assert "SyntaxError: invalid syntax on line 3" in seen_prompts[0]

    def test_monitor_stop_maps_to_failure(self):
        class ChattyStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                return LLMResponse(content="still thinking...")

        worker = WorkerAgent(
            role="backend_dev",
            bus=InMemoryBus(),
            matrix=InMemoryDataMatrix(HashEmbedder()),
            config=OrchestrationConfig(),
            agent_config=make_agent_config(max_iterations=3),
            llm_factory=ChattyStub,
        )
        update = worker.handle(make_payload())
        assert update.status == UpdateStatus.FAILURE.value
        assert update.error_log

    def test_crash_becomes_structured_failure_with_traceback(self):
        worker = make_worker([])

        def explode():
            raise RuntimeError("llm factory exploded")

        worker._llm_factory = explode
        update = worker.handle(make_payload())
        assert update.status == UpdateStatus.FAILURE.value
        assert "llm factory exploded" in update.error_log
        assert "Traceback" in update.error_log

    def test_completed_work_past_deadline_is_flagged_not_failed(self):
        # Failing already-finished work would trigger a destructive retry of
        # its side effects; the overshoot is reported as a metric instead.
        import time

        class SlowStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                time.sleep(0.05)
                return LLMResponse(content=None, tool_name="finish",
                                   tool_args={"conclusion": "done but too slow"})

        worker = make_worker([])
        worker._llm_factory = SlowStub
        payload = make_payload(constraints=Constraints(timeout_ms=1))
        update = worker.handle(payload)
        assert update.status == UpdateStatus.SUCCESS.value
        assert update.metrics.get("deadline_exceeded") is True

    def test_token_budget_flows_into_agent_config(self):
        class HungryStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                return LLMResponse(content="working...", usage={"input": 900, "output": 200})

        worker = make_worker([])
        worker._llm_factory = HungryStub
        update = worker.handle(make_payload(
            constraints=Constraints(max_compute_tokens=2000)
        ))
        assert update.status == UpdateStatus.FAILURE.value
        assert "Token budget exhausted" in update.error_log


class TestWorkerOnBus:
    def test_consumes_payload_and_publishes_to_callback_queue(self):
        import threading
        import time

        bus = InMemoryBus()
        worker = make_worker(
            [LLMResponse(content=None, tool_name="finish",
                         tool_args={"conclusion": "bus result"})],
            bus=bus,
        )
        received = []
        stop = threading.Event()
        collector = threading.Thread(
            target=bus.consume, args=("q.critic.review", received.append, stop)
        )
        collector.start()
        worker.start()
        try:
            bus.publish(worker.queue, make_payload().to_json())
            deadline = time.monotonic() + 5
            while not received and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            worker.stop()
            stop.set()
            collector.join(timeout=2)

        assert received, "worker never published a StateUpdate"
        from mythos.orchestration.schemas import StateUpdate
        update = StateUpdate.from_json(received[0])
        assert update.status == UpdateStatus.SUCCESS.value
        assert update.summary == "bus result"
