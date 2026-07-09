"""
tests/orchestration/test_schemas.py
-----------------------------------
Round-trip and strict-validation tests for the M2M message schemas.
"""
import json

import pytest

from mythos.orchestration.schemas import (
    Constraints,
    MemoryNode,
    SchemaError,
    StateUpdate,
    TargetAgent,
    TaskParameters,
    TaskPayload,
    UpdateStatus,
    TRUST_SYSTEM,
    new_task_id,
    new_trace_id,
)


def make_payload(**overrides) -> TaskPayload:
    defaults = dict(
        system_instruction="EXECUTE_SUBTASK",
        trace_id=new_trace_id(),
        task_id=new_task_id(),
        orchestrator_node="orchestrator-0",
        target_agent=TargetAgent(role="backend_dev"),
        task_parameters=TaskParameters(
            objective="Write /tmp/fib.py",
            context_pointers=["node-1", "node-2"],
        ),
        constraints=Constraints(forbidden_modules=["run_shell"]),
        callback_queue="q.critic.review",
    )
    defaults.update(overrides)
    return TaskPayload(**defaults)


class TestTaskPayload:
    def test_round_trip(self):
        payload = make_payload(attempt=2, error_log="Traceback: boom")
        parsed = TaskPayload.from_json(payload.to_json())
        assert parsed == payload

    def test_context_pointers_survive(self):
        parsed = TaskPayload.from_json(make_payload().to_json())
        assert parsed.task_parameters.context_pointers == ["node-1", "node-2"]
        assert parsed.constraints.forbidden_modules == ["run_shell"]

    def test_unknown_instruction_rejected(self):
        raw = json.loads(make_payload().to_json())
        raw["system_instruction"] = "DO_SOMETHING_ELSE"
        with pytest.raises(SchemaError):
            TaskPayload.from_json(json.dumps(raw))

    def test_missing_trace_id_rejected(self):
        raw = json.loads(make_payload().to_json())
        del raw["trace_id"]
        with pytest.raises(SchemaError):
            TaskPayload.from_json(json.dumps(raw))

    def test_non_json_rejected(self):
        with pytest.raises(SchemaError):
            TaskPayload.from_json("this is not json")

    def test_non_object_rejected(self):
        with pytest.raises(SchemaError):
            TaskPayload.from_json("[1, 2, 3]")

    def test_defaults_applied(self):
        raw = json.loads(make_payload().to_json())
        del raw["constraints"]
        del raw["attempt"]
        parsed = TaskPayload.from_json(json.dumps(raw))
        assert parsed.attempt == 1
        assert parsed.constraints == Constraints()


class TestStateUpdate:
    def test_round_trip_with_embedded_payload(self):
        payload = make_payload()
        update = StateUpdate(
            trace_id=payload.trace_id,
            task_id=payload.task_id,
            agent_role="backend_dev",
            status=UpdateStatus.SUCCESS.value,
            result_pointers=["artifact-1"],
            summary="done",
            metrics={"wall_ms": 12},
            task_payload=json.loads(payload.to_json()),
        )
        parsed = StateUpdate.from_json(update.to_json())
        assert parsed == update
        assert parsed.payload() == payload

    def test_payload_none_when_not_carried(self):
        update = StateUpdate(
            trace_id="t", task_id="k", agent_role="critic",
            status=UpdateStatus.VALIDATED.value,
        )
        assert StateUpdate.from_json(update.to_json()).payload() is None

    def test_unknown_status_rejected(self):
        update = StateUpdate(
            trace_id="t", task_id="k", agent_role="critic",
            status=UpdateStatus.VALIDATED.value,
        )
        raw = json.loads(update.to_json())
        raw["status"] = "MAYBE"
        with pytest.raises(SchemaError):
            StateUpdate.from_json(json.dumps(raw))

    def test_error_log_preserved_verbatim(self):
        trace = "Traceback (most recent call last):\n  File \"x.py\", line 1\nBoom\n"
        update = StateUpdate(
            trace_id="t", task_id="k", agent_role="backend_dev",
            status=UpdateStatus.FAILURE.value, error_log=trace,
        )
        assert StateUpdate.from_json(update.to_json()).error_log == trace


class TestMemoryNode:
    def test_create_sets_metadata(self):
        node = MemoryNode.create(
            node_type="system_instruction",
            content="Never fabricate data.",
            source="orchestrator",
            trust_score=TRUST_SYSTEM,
            verbatim_required=True,
        )
        assert node.node_id
        assert node.trust_score == TRUST_SYSTEM
        assert node.verbatim_required is True
        assert node.metadata["source"] == "orchestrator"
        assert "timestamp" in node.metadata

    def test_dict_round_trip(self):
        node = MemoryNode.create(
            node_type="artifact",
            content="print('hi')",
            source="agent:backend_dev",
            edges=[{"relation": "produced_for", "target_id": "goal-1"}],
        )
        assert MemoryNode.from_dict(node.to_dict()) == node

    def test_missing_id_rejected(self):
        with pytest.raises(SchemaError):
            MemoryNode.from_dict({"node_type": "artifact", "content": "x"})
