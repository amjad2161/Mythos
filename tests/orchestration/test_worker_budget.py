"""
tests/orchestration/test_worker_budget.py
-----------------------------------------
Real token-budget enforcement in the worker path and token metrics on
StateUpdates.
"""
from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.bus import InMemoryBus
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.roles import ROLE_TOOLS
from mythos.orchestration.schemas import Constraints, UpdateStatus
from mythos.orchestration.worker import WorkerAgent
from mythos.orchestration.workflows import get_workflow

from .conftest import make_agent_config, make_orch_config, make_payload


def make_worker(llm_factory):
    return WorkerAgent(
        role="backend_dev",
        bus=InMemoryBus(),
        matrix=InMemoryDataMatrix(HashEmbedder()),
        config=make_orch_config(),
        agent_config=make_agent_config(),
        llm_factory=llm_factory,
    )


def test_token_budget_exhaustion_becomes_failure():
    class HungryStub(StubLLM):
        def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
            return LLMResponse(content="thinking...", usage={"input": 5000, "output": 1000})

    worker = make_worker(HungryStub)
    payload = make_payload(constraints=Constraints(max_compute_tokens=10_000))
    update = worker.handle(payload)
    assert update.status == UpdateStatus.FAILURE.value
    assert "Token budget exhausted" in update.error_log


def test_token_metrics_reported_on_success():
    def factory():
        return StubLLM([
            LLMResponse(content=None, tool_name="finish",
                        tool_args={"conclusion": "done"},
                        usage={"input": 100, "output": 20, "cache_read": 50}),
        ])

    worker = make_worker(factory)
    update = worker.handle(make_payload())
    assert update.status == UpdateStatus.SUCCESS.value
    tokens = update.metrics["tokens"]
    assert tokens["input"] == 100
    assert tokens["output"] == 20
    assert tokens["cache_read"] == 50


def test_new_roles_and_workflow_resolve():
    for role in ("researcher", "navigator", "voice"):
        assert role in ROLE_TOOLS
    workflow = get_workflow("route_plan")
    assert [s.role for s in workflow.steps] == ["navigator", "voice"]
