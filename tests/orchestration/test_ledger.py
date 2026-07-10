"""
tests/orchestration/test_ledger.py
----------------------------------
TaskLedger: durable, observable per-goal progress in the Data Matrix.
"""
import pytest

from mythos.orchestration.ledger import TaskLedger
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.schemas import SchemaError


@pytest.fixture()
def matrix():
    return InMemoryDataMatrix(HashEmbedder())


@pytest.fixture()
def ledger(matrix):
    return TaskLedger(matrix)


def create(ledger):
    return ledger.create(
        trace_id="trace-1",
        goal="build the thing",
        steps=[
            {"role": "backend_dev", "objective": "implement"},
            {"role": "voice", "objective": "announce"},
        ],
        goal_node_id="goal-node",
    )


def test_create_and_read_round_trip(ledger):
    ledger_id = create(ledger)
    document = ledger.read(ledger_id)
    assert document["goal"] == "build the thing"
    assert [s["status"] for s in document["steps"]] == ["pending", "pending"]
    assert document["steps"][1]["role"] == "voice"


def test_stable_node_id_across_updates(ledger, matrix):
    ledger_id = create(ledger)
    ledger.mark_dispatched(ledger_id, 0, "task-abc")
    ledger.mark_terminal(ledger_id, 0, "validated", attempts=2, summary="done")
    document = ledger.read(ledger_id)
    assert document["steps"][0] == {
        "index": 0, "role": "backend_dev", "objective": "implement",
        "task_id": "task-abc", "status": "validated", "attempts": 2,
        "summary": "done",
    }
    # One node in the matrix carries the whole history (stable id).
    assert len(matrix.get([ledger_id])) == 1


def test_ledger_tracks_goal_node(ledger, matrix):
    ledger_id = create(ledger)
    [node] = matrix.get([ledger_id])
    assert node.edges == [{"relation": "tracks", "target_id": "goal-node"}]
    assert node.node_type == "ledger"


def test_read_missing_ledger_raises(ledger):
    with pytest.raises(SchemaError):
        ledger.read("no-such-node")


def test_mark_out_of_range_raises(ledger):
    ledger_id = create(ledger)
    with pytest.raises(SchemaError):
        ledger.mark_dispatched(ledger_id, 7, "task-x")


def test_orchestrator_updates_ledger_end_to_end():
    """The e2e path: dispatched -> validated transitions recorded."""
    from mythos.llm import LLMResponse, StubLLM
    from mythos.orchestration.runtime import SwarmRuntime
    from mythos.orchestration.workflows import Workflow, WorkflowStep
    from mythos.orchestration.bus import InMemoryBus

    from .conftest import make_agent_config, make_orch_config

    matrix = InMemoryDataMatrix(HashEmbedder())
    workflow = Workflow(
        name="ledger_demo",
        steps=[WorkflowStep(role="backend_dev", objective_template="Do: {goal}",
                            validation_command_template="true")],
    )
    runtime = SwarmRuntime(
        config=make_orch_config(),
        agent_config=make_agent_config(),
        workflow=workflow,
        bus=InMemoryBus(),
        matrix=matrix,
        llm_factories={"backend_dev": lambda: StubLLM([
            LLMResponse(content=None, tool_name="finish",
                        tool_args={"conclusion": "did it"}),
        ])},
    )
    try:
        runtime.run("the goal")
    finally:
        runtime.shutdown()

    ledgers = [
        node for node in matrix._nodes.values() if node.node_type == "ledger"
    ]
    assert len(ledgers) == 1
    document = TaskLedger(matrix).read(ledgers[0].node_id)
    [step] = document["steps"]
    assert step["status"] == "validated"
    assert step["task_id"].startswith("task_")
    assert step["attempts"] == 1
