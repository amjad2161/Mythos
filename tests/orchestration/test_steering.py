"""
tests/orchestration/test_steering.py
------------------------------------
Cooperative cancel / steering: request_cancel stops an in-progress goal at the
next dispatch boundary and the run reports it as cancelled.
"""
from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.bus import InMemoryBus
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.runtime import SwarmRuntime
from mythos.orchestration.workflows import Workflow, WorkflowStep

from .conftest import make_agent_config, make_orch_config


def _runtime(workflow, factories):
    return SwarmRuntime(
        config=make_orch_config(),
        agent_config=make_agent_config(),
        workflow=workflow,
        bus=InMemoryBus(),
        matrix=InMemoryDataMatrix(HashEmbedder()),
        llm_factories=factories,
    )


def test_cancel_flags_reset_and_report():
    orch_wf = Workflow(name="c", steps=[
        WorkflowStep(role="backend_dev", objective_template="Do: {goal}",
                     validation_command_template="true"),
    ])
    factories = {"backend_dev": lambda: StubLLM([
        LLMResponse(content=None, tool_name="finish", tool_args={"conclusion": "done"}),
    ])}
    runtime = _runtime(orch_wf, factories)
    orch = runtime.orchestrator
    assert not orch.was_cancelled()

    # Trip the cancel the moment the first subtask is dispatched; the loop then
    # stops at the next dispatch boundary and reports the run cancelled.
    original = orch._dispatch

    def cancelling_dispatch(*args, **kwargs):
        payload = original(*args, **kwargs)
        orch.request_cancel()
        return payload

    orch._dispatch = cancelling_dispatch
    try:
        conclusion = runtime.run("stop me")
    finally:
        runtime.shutdown()

    assert conclusion.startswith("Goal cancelled by user.")
    assert orch.was_cancelled()


def test_uncancelled_run_completes_normally():
    workflow = Workflow(name="ok", steps=[
        WorkflowStep(role="backend_dev", objective_template="Do: {goal}",
                     validation_command_template="true"),
    ])
    factories = {"backend_dev": lambda: StubLLM([
        LLMResponse(content=None, tool_name="finish", tool_args={"conclusion": "done"}),
    ])}
    runtime = _runtime(workflow, factories)
    try:
        conclusion = runtime.run("just run")
    finally:
        runtime.shutdown()
    # cancel state is cleared per run, so a normal run never reports cancelled
    assert not runtime.orchestrator.was_cancelled()
    assert "cancelled" not in conclusion.lower()
