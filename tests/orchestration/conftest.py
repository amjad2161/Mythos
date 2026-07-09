"""
tests/orchestration/conftest.py
-------------------------------
Shared factories for the orchestration test suite (imported directly by the
test modules; kept in one place so a schema/config change is edited once).
"""
from mythos.config import MythosConfig
from mythos.orchestration.config import OrchestrationConfig
from mythos.orchestration.schemas import (
    TargetAgent,
    TaskParameters,
    TaskPayload,
)


def make_agent_config(**overrides) -> MythosConfig:
    """Stub-provider MythosConfig used by every scripted-agent test."""
    defaults = dict(
        llm_provider="stub",
        llm_api_key="unused",
        verbose=False,
        persist_memory=False,
    )
    defaults.update(overrides)
    return MythosConfig(**defaults)


def make_orch_config(**overrides) -> OrchestrationConfig:
    """In-memory-backends OrchestrationConfig used across the suite."""
    defaults = dict(
        bus_backend="inmemory",
        matrix_backend="inmemory",
        embedder="hash",
        result_timeout_s=15.0,
        verbose=False,
    )
    defaults.update(overrides)
    return OrchestrationConfig(**defaults)


def make_payload(**overrides) -> TaskPayload:
    """A canonical EXECUTE_SUBTASK TaskPayload with overridable fields."""
    defaults = dict(
        system_instruction="EXECUTE_SUBTASK",
        trace_id="trace-1",
        task_id="task-1",
        orchestrator_node="orchestrator-0",
        target_agent=TargetAgent(role="backend_dev"),
        task_parameters=TaskParameters(objective="Do the thing"),
        callback_queue="q.critic.review",
    )
    defaults.update(overrides)
    return TaskPayload(**defaults)
