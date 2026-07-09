"""
tests/integration/test_swarm_live.py
------------------------------------
Full swarm over live RabbitMQ + Qdrant with scripted StubLLMs: the identical
flow the offline e2e test runs in-memory, now across real infrastructure.
"""
import pytest

from mythos.config import MythosConfig
from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.config import OrchestrationConfig
from mythos.orchestration.runtime import SwarmRuntime
from mythos.orchestration.workflows import Workflow, WorkflowStep

pytestmark = pytest.mark.integration


def test_goal_round_trip_over_live_services(broker_url, qdrant_url, unique_name, tmp_path):
    target = tmp_path / "live.txt"
    workflow = Workflow(
        name="live_demo",
        steps=[
            WorkflowStep(
                role="backend_dev",
                objective_template="Write 'hello' to {goal}",
                validation_command_template=f"grep -q hello {target}",
            ),
        ],
    )

    def worker_factory():
        return StubLLM([
            LLMResponse(content=None, tool_name="write_file",
                        tool_args={"path": str(target), "content": "hello\n"}),
            LLMResponse(content=None, tool_name="finish",
                        tool_args={"conclusion": f"wrote hello to {target}"}),
        ])

    runtime = SwarmRuntime(
        config=OrchestrationConfig(
            bus_backend="rabbitmq",
            matrix_backend="qdrant",
            broker_url=broker_url,
            qdrant_url=qdrant_url,
            matrix_collection=f"mythos_live_{unique_name}",
            embedder="hash",
            result_timeout_s=60.0,
            verbose=False,
        ),
        agent_config=MythosConfig(llm_provider="stub", llm_api_key="unused", verbose=False),
        workflow=workflow,
        llm_factories={"backend_dev": worker_factory},
    )
    try:
        conclusion = runtime.run(str(target))
    finally:
        try:
            runtime.matrix._client.delete_collection(runtime.matrix._collection)
        finally:
            runtime.shutdown()

    assert target.read_text() == "hello\n"
    assert "hello" in conclusion
