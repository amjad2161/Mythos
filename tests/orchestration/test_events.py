"""
tests/orchestration/test_events.py
----------------------------------
The real-time event hub: fan-out, lossy backpressure, history replay, and
that the orchestrator emits lifecycle events + resolves resource dependencies.
"""
import threading

from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.bus import InMemoryBus
from mythos.orchestration.events import EventHub
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.runtime import SwarmRuntime
from mythos.orchestration.workflows import Workflow, WorkflowStep

from .conftest import make_agent_config, make_orch_config


class TestEventHub:
    def test_fanout_to_multiple_subscribers(self):
        hub = EventHub()
        a, b = hub.subscribe(), hub.subscribe()
        hub.emit("test.event", role="x", detail_value=1)
        stop = threading.Event()
        stop.set()  # drain-only: stream exits immediately after buffered items
        # pull directly from the underlying queues
        ea = a._q.get_nowait()
        eb = b._q.get_nowait()
        assert ea.kind == eb.kind == "test.event"
        assert ea.seq == eb.seq

    def test_sequence_and_timestamp_stamped(self):
        hub = EventHub()
        e1 = hub.emit("a")
        e2 = hub.emit("b")
        assert e2.seq == e1.seq + 1
        assert e1.ts_ms > 0

    def test_lossy_backpressure_drops_oldest(self):
        hub = EventHub(per_subscriber_buffer=3)
        sub = hub.subscribe()
        for i in range(10):
            hub.emit("e", n=i)
        drained = []
        try:
            while True:
                drained.append(sub._q.get_nowait())
        except Exception:  # noqa: BLE001 – queue.Empty
            pass
        assert len(drained) == 3               # bounded
        assert drained[-1].detail["n"] == 9    # newest survives

    def test_history_replay(self):
        hub = EventHub(history=5)
        for i in range(8):
            hub.emit("e", n=i)
        recent = hub.recent(5)
        assert [e.detail["n"] for e in recent] == [3, 4, 5, 6, 7]

    def test_unsubscribe_closes_stream(self):
        hub = EventHub()
        sub = hub.subscribe()
        hub.unsubscribe(sub)
        # a closed subscription yields the sentinel None then stops
        stop = threading.Event()
        assert list(sub.stream(stop)) == []


class TestOrchestratorEmitsEvents:
    def _run_and_collect(self, workflow, factories):
        runtime = SwarmRuntime(
            config=make_orch_config(),
            agent_config=make_agent_config(),
            workflow=workflow,
            bus=InMemoryBus(),
            matrix=InMemoryDataMatrix(HashEmbedder()),
            llm_factories=factories,
        )
        sub = runtime.events.subscribe()
        try:
            conclusion = runtime.run("do the thing")
        finally:
            runtime.shutdown()
        # Drain the subscription synchronously after the run (no racing thread).
        hub_events = []
        try:
            while True:
                ev = sub._q.get_nowait()
                if ev is not None and ev.kind != "heartbeat":
                    hub_events.append(ev.kind)
        except Exception:  # noqa: BLE001 – queue.Empty
            pass
        return conclusion, hub_events

    def test_lifecycle_events_emitted(self):
        workflow = Workflow(
            name="ev",
            steps=[WorkflowStep(role="backend_dev", objective_template="Do: {goal}",
                                validation_command_template="true")],
        )
        factories = {"backend_dev": lambda: StubLLM([
            LLMResponse(content=None, tool_name="finish",
                        tool_args={"conclusion": "done"}),
        ])}
        conclusion, events = self._run_and_collect(workflow, factories)
        assert "goal.started" in events
        assert "task.dispatched" in events
        assert "task.validated" in events
        assert "goal.completed" in events


class TestResourceDependencies:
    def test_predecessor_pointers_flow_to_dependent(self):
        """A dependent step's context_pointers include its predecessor's
        artifact node id (HuggingGPT-style explicit resource dependency)."""
        seen_pointers = {}

        def recording_factory(role):
            def make():
                class RecordingStub(StubLLM):
                    def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                        # capture how many matrix nodes were fused into the prompt
                        import json as _json
                        seen_pointers.setdefault(role, []).append(_json.dumps(messages))
                        return LLMResponse(content=None, tool_name="finish",
                                           tool_args={"conclusion": f"{role} done"})
                return RecordingStub()
            return make

        workflow = Workflow(
            name="chain",
            steps=[
                WorkflowStep(role="backend_dev", objective_template="produce ALPHA_ARTIFACT",
                             validation_command_template="true"),
                WorkflowStep(role="researcher", objective_template="consume the prior result",
                             validation_command_template="true", depends_on=[0]),
            ],
        )
        runtime = SwarmRuntime(
            config=make_orch_config(),
            agent_config=make_agent_config(),
            workflow=workflow,
            bus=InMemoryBus(),
            matrix=InMemoryDataMatrix(HashEmbedder()),
            llm_factories={
                "backend_dev": recording_factory("backend_dev"),
                "researcher": recording_factory("researcher"),
            },
        )
        try:
            runtime.run("chain goal")
        finally:
            runtime.shutdown()
        # The researcher's fused prompt must contain the backend_dev artifact
        # content ("backend_dev done") pulled in via the dependency pointer.
        researcher_prompt = seen_pointers["researcher"][0]
        assert "backend_dev done" in researcher_prompt
