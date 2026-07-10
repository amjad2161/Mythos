"""
tests/orchestration/test_end_to_end_inmemory.py
-----------------------------------------------
Full swarm end-to-end over the in-memory drivers: orchestrator -> worker ->
critic -> orchestrator, with scripted StubLLMs and a real artifact on disk.
"""
import os

from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.bus import InMemoryBus
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.runtime import SwarmRuntime
from mythos.orchestration.workflows import Workflow, WorkflowStep

from .conftest import make_agent_config, make_orch_config


def make_runtime(workflow, llm_factories):
    return SwarmRuntime(
        config=make_orch_config(),
        agent_config=make_agent_config(),
        workflow=workflow,
        bus=InMemoryBus(),
        matrix=InMemoryDataMatrix(HashEmbedder()),
        llm_factories=llm_factories,
    )


def test_happy_path_writes_file_and_validates_mechanically(tmp_path):
    """Worker writes a real file; the critic's validation command checks it."""
    target = tmp_path / "fib.py"
    workflow = Workflow(
        name="demo",
        steps=[
            WorkflowStep(
                role="backend_dev",
                objective_template="Write a fibonacci script to {goal}",
                validation_command_template=f"python {target}",
            ),
        ],
    )

    worker_script = [
        LLMResponse(
            content="Writing the script.",
            tool_name="write_file",
            tool_args={
                "path": str(target),
                "content": "print([1, 1, 2, 3, 5, 8, 13, 21, 34, 55])\n",
            },
        ),
        LLMResponse(
            content=None,
            tool_name="finish",
            tool_args={"conclusion": f"Wrote fibonacci script to {target}."},
        ),
    ]

    runtime = make_runtime(
        workflow,
        llm_factories={"backend_dev": lambda: StubLLM(list(worker_script))},
    )
    try:
        conclusion = runtime.run(str(target))
    finally:
        runtime.shutdown()

    assert os.path.exists(target)
    assert "fibonacci" in conclusion.lower()


def test_retry_loop_recovers_from_bad_first_attempt(tmp_path):
    """First attempt fails mechanical validation; the retry fixes it."""
    target = tmp_path / "answer.txt"
    check = f"grep -q '42' {target}"
    workflow = Workflow(
        name="demo_retry",
        steps=[
            WorkflowStep(
                role="backend_dev",
                objective_template="Write the answer to {goal}",
                validation_command_template=check,
            ),
        ],
    )

    attempts = []
    runs = [
        # Attempt 1: writes the wrong content -> `grep 42` fails.
        [
            LLMResponse(content=None, tool_name="write_file",
                        tool_args={"path": str(target), "content": "wrong\n"}),
            LLMResponse(content=None, tool_name="finish",
                        tool_args={"conclusion": "wrote (wrong) answer"}),
        ],
        # Attempt 2 (RETRY with error_log in the prompt): writes 42.
        [
            LLMResponse(content=None, tool_name="write_file",
                        tool_args={"path": str(target), "content": "42\n"}),
            LLMResponse(content=None, tool_name="finish",
                        tool_args={"conclusion": "wrote the answer 42"}),
        ],
    ]

    def worker_factory():
        attempts.append(len(attempts) + 1)
        return StubLLM(list(runs.pop(0)))

    runtime = make_runtime(workflow, llm_factories={"backend_dev": worker_factory})
    try:
        conclusion = runtime.run(str(target))
    finally:
        runtime.shutdown()

    assert attempts == [1, 2]                      # exactly one autonomous retry
    assert target.read_text() == "42\n"
    assert "42" in conclusion


def test_failure_after_retries_exhausted(tmp_path):
    """A worker that never satisfies validation ends in a reported failure."""
    workflow = Workflow(
        name="demo_fail",
        steps=[
            WorkflowStep(
                role="backend_dev",
                objective_template="Impossible: {goal}",
                validation_command_template="false",  # always fails
            ),
        ],
    )

    def worker_factory():
        return StubLLM([
            LLMResponse(content=None, tool_name="finish",
                        tool_args={"conclusion": "pretending it is done"}),
        ])

    runtime = make_runtime(workflow, llm_factories={"backend_dev": worker_factory})
    runtime.config.max_attempts = 2
    try:
        conclusion = runtime.run("something unachievable")
    finally:
        runtime.shutdown()

    assert conclusion.startswith("Goal failed.")
    assert "[exit code 1]" in conclusion
