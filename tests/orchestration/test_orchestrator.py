"""
tests/orchestration/test_orchestrator.py
----------------------------------------
Orchestrator behaviour: matrix seeding, workflow decomposition, dispatch,
result collection, and failure reporting.  The worker/critic are simulated by
a scripted responder on the task queue so these tests exercise the
orchestrator in isolation.
"""
import threading

import pytest

from mythos.orchestration.bus import CRITIC_QUEUE, RESULTS_QUEUE, InMemoryBus, task_queue
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.orchestrator import Orchestrator, SwarmTimeoutError
from mythos.orchestration.schemas import MemoryNode, StateUpdate, TaskPayload, UpdateStatus
from mythos.orchestration.workflows import (
    Workflow,
    WorkflowStep,
    get_workflow,
)

from .conftest import make_orch_config


def make_config(**overrides) -> "OrchestrationConfig":
    return make_orch_config(result_timeout_s=overrides.pop("result_timeout_s", 5.0), **overrides)


class ScriptedSwarm:
    """Plays worker+critic: answers each dispatched payload from a script."""

    def __init__(self, bus, matrix, outcomes):
        self._bus = bus
        self._matrix = matrix
        self._outcomes = list(outcomes)  # (status, content) per dispatch
        self._stop = threading.Event()
        self._threads = []

    def listen(self, role):
        thread = threading.Thread(
            target=self._bus.consume,
            args=(task_queue(role), self._respond, self._stop),
            daemon=True,
        )
        thread.start()
        self._threads.append(thread)

    def _respond(self, body):
        payload = TaskPayload.from_json(body)
        status, content = self._outcomes.pop(0)
        pointers = []
        if status == UpdateStatus.VALIDATED:
            node = MemoryNode.create(
                node_type="artifact", content=content, source="scripted"
            )
            self._matrix.upsert(node)
            pointers = [node.node_id]
        self._bus.publish(
            RESULTS_QUEUE,
            StateUpdate(
                trace_id=payload.trace_id,
                task_id=payload.task_id,
                agent_role="critic",
                status=status.value,
                result_pointers=pointers,
                summary=content,
                error_log=content if status == UpdateStatus.FAILURE else None,
            ).to_json(),
        )

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)


def run_goal(workflow, outcomes, goal="build the fibonacci script"):
    bus = InMemoryBus()
    matrix = InMemoryDataMatrix(HashEmbedder())
    swarm = ScriptedSwarm(bus, matrix, outcomes)
    for role in {s.role for s in workflow.steps}:
        swarm.listen(role)
    orchestrator = Orchestrator(bus, matrix, make_config(), workflow)
    orchestrator.start()
    try:
        return orchestrator.run(goal), matrix, bus
    finally:
        orchestrator.stop()
        swarm.stop()


class TestWorkflows:
    def test_builtin_lookup(self):
        assert get_workflow("code_delivery").steps[0].role == "backend_dev"

    def test_unknown_workflow_raises(self):
        with pytest.raises(ValueError):
            get_workflow("does_not_exist")

    def test_objective_substitutes_goal(self):
        step = WorkflowStep(role="backend_dev", objective_template="Implement: {goal}")
        assert step.objective("X") == "Implement: X"

    def test_validation_command_shell_quotes_goal(self):
        # The goal is user text substituted into a shell=True command - a
        # crafted goal must not inject shell syntax.
        step = WorkflowStep(
            role="backend_dev",
            objective_template="{goal}",
            validation_command_template="grep -q {goal} /tmp/out",
        )
        command = step.validation_command("x'; rm -rf /tmp/pwned; echo '")
        import shlex
        # The quoted goal round-trips as ONE argv token - no injection.
        assert shlex.split(command)[2] == "x'; rm -rf /tmp/pwned; echo '"

    def test_literal_step_leaves_braces_intact(self):
        step = WorkflowStep(
            role="backend_dev",
            objective_template="print({'a': 1}) then {goal}",
            literal=True,
        )
        assert step.objective("X") == "print({'a': 1}) then {goal}"


class TestOrchestratorRun:
    def test_single_step_success(self):
        workflow = get_workflow("code_delivery")
        conclusion, matrix, _ = run_goal(
            workflow, [(UpdateStatus.VALIDATED, "artifact content here")]
        )
        assert conclusion == "artifact content here"

    def test_matrix_seeded_with_system_and_goal_nodes(self):
        workflow = get_workflow("code_delivery")
        _, matrix, _ = run_goal(
            workflow, [(UpdateStatus.VALIDATED, "ok")], goal="my unique goal text"
        )
        types = {n.node_type for n in matrix._nodes.values()}
        assert "system_instruction" in types
        assert "goal" in types
        goal_nodes = [
            n for n in matrix._nodes.values() if n.node_type == "goal"
        ]
        assert goal_nodes[0].content == "my unique goal text"  # verbatim
        assert goal_nodes[0].edges[0]["relation"] == "refines"

    def test_multi_step_sequential_dispatch(self):
        workflow = Workflow(
            name="two_steps",
            steps=[
                WorkflowStep(role="backend_dev", objective_template="Step one: {goal}"),
                WorkflowStep(role="backend_dev", objective_template="Step two: {goal}"),
            ],
        )
        conclusion, _, _ = run_goal(
            workflow,
            [(UpdateStatus.VALIDATED, "first"), (UpdateStatus.VALIDATED, "second")],
        )
        assert conclusion == "first | second"

    def test_terminal_failure_reported(self):
        workflow = get_workflow("code_delivery")
        conclusion, _, _ = run_goal(
            workflow, [(UpdateStatus.FAILURE, "retries exhausted: boom")]
        )
        assert conclusion.startswith("Goal failed.")
        assert "boom" in conclusion

    def test_failure_stops_downstream_steps(self):
        workflow = Workflow(
            name="two_steps",
            steps=[
                WorkflowStep(role="backend_dev", objective_template="one: {goal}"),
                WorkflowStep(role="backend_dev", objective_template="two: {goal}"),
            ],
        )
        conclusion, _, _ = run_goal(
            workflow,
            [(UpdateStatus.FAILURE, "step one failed")],
        )
        assert conclusion.startswith("Goal failed.")
        assert "step two" not in conclusion.lower()

    def test_dispatched_payload_shape(self):
        captured = []
        bus = InMemoryBus()
        matrix = InMemoryDataMatrix(HashEmbedder())
        stop = threading.Event()

        def capture_and_answer(body):
            payload = TaskPayload.from_json(body)
            captured.append(payload)
            bus.publish(
                RESULTS_QUEUE,
                StateUpdate(
                    trace_id=payload.trace_id,
                    task_id=payload.task_id,
                    agent_role="critic",
                    status=UpdateStatus.VALIDATED.value,
                    summary="ok",
                ).to_json(),
            )

        listener = threading.Thread(
            target=bus.consume,
            args=(task_queue("backend_dev"), capture_and_answer, stop),
            daemon=True,
        )
        listener.start()

        orchestrator = Orchestrator(
            bus, matrix, make_config(), get_workflow("code_delivery")
        )
        orchestrator.start()
        try:
            orchestrator.run("the goal")
        finally:
            orchestrator.stop()
            stop.set()
            listener.join(timeout=2)

        [payload] = captured
        assert payload.system_instruction == "EXECUTE_SUBTASK"
        assert payload.orchestrator_node == "orchestrator-0"
        assert payload.callback_queue == CRITIC_QUEUE
        assert payload.target_agent.role == "backend_dev"
        assert payload.task_parameters.context_pointers  # points at the goal node
        assert payload.trace_id.startswith("trace_")
        assert payload.task_id.startswith("task_")

    def test_timeout_raises(self):
        bus = InMemoryBus()
        matrix = InMemoryDataMatrix(HashEmbedder())
        orchestrator = Orchestrator(
            bus,
            matrix,
            make_config(result_timeout_s=0.2),
            get_workflow("code_delivery"),
        )
        orchestrator.start()
        try:
            with pytest.raises(SwarmTimeoutError):
                orchestrator.run("nobody is listening")
        finally:
            orchestrator.stop()

    def test_independent_steps_dispatch_concurrently(self):
        """Two independent steps are BOTH dispatched before either result is
        answered; a join step depending on both runs only afterwards."""
        import time

        bus = InMemoryBus()
        matrix = InMemoryDataMatrix(HashEmbedder())
        stop = threading.Event()
        received = []          # payloads seen by the fake swarm
        lock = threading.Lock()

        def branches_seen():
            with lock:
                return sum(
                    1 for p in received
                    if p.task_parameters.objective.startswith("branch")
                )

        def responder(body):
            payload = TaskPayload.from_json(body)
            with lock:
                received.append(payload)
            if payload.task_parameters.objective.startswith("branch"):
                # Answer the branch tasks only once BOTH were dispatched -
                # proof the orchestrator did not wait for one before sending
                # the other.
                deadline = time.monotonic() + 5
                while branches_seen() < 2:
                    if time.monotonic() > deadline:
                        raise AssertionError("second branch was never dispatched")
                    time.sleep(0.01)
            node = MemoryNode.create(
                node_type="artifact",
                content=f"done: {payload.task_parameters.objective}",
                source="scripted",
            )
            matrix.upsert(node)
            bus.publish(
                RESULTS_QUEUE,
                StateUpdate(
                    trace_id=payload.trace_id,
                    task_id=payload.task_id,
                    agent_role="critic",
                    status=UpdateStatus.VALIDATED.value,
                    result_pointers=[node.node_id],
                    summary=payload.task_parameters.objective,
                ).to_json(),
            )

        # Two consumers on the role queue (= two workers of the same role),
        # so the branch handlers can genuinely overlap.
        listeners = [
            threading.Thread(
                target=bus.consume,
                args=(task_queue("backend_dev"), responder, stop),
                daemon=True,
            )
            for _ in range(2)
        ]
        for listener in listeners:
            listener.start()

        workflow = Workflow(
            name="diamond",
            steps=[
                WorkflowStep(role="backend_dev", objective_template="branch A: {goal}",
                             depends_on=[]),
                WorkflowStep(role="backend_dev", objective_template="branch B: {goal}",
                             depends_on=[]),
                WorkflowStep(role="backend_dev", objective_template="join: {goal}",
                             depends_on=[0, 1]),
            ],
        )
        orchestrator = Orchestrator(bus, matrix, make_config(), workflow)
        orchestrator.start()
        try:
            conclusion = orchestrator.run("the goal")
        finally:
            orchestrator.stop()
            stop.set()
            for listener in listeners:
                listener.join(timeout=2)

        assert "done: branch A: the goal" in conclusion
        assert "done: branch B: the goal" in conclusion
        assert "done: join: the goal" in conclusion
        # The join must have been dispatched last, after both branches.
        objectives = [p.task_parameters.objective for p in received]
        assert objectives.index("join: the goal") == 2

    def test_failed_branch_blocks_dependents(self):
        workflow = Workflow(
            name="fail_branch",
            steps=[
                WorkflowStep(role="backend_dev", objective_template="one: {goal}",
                             depends_on=[]),
                WorkflowStep(role="backend_dev", objective_template="two: {goal}",
                             depends_on=[0]),
            ],
        )
        conclusion, _, _ = run_goal(
            workflow, [(UpdateStatus.FAILURE, "branch one broke")]
        )
        assert conclusion.startswith("Goal failed.")
        assert "branch one broke" in conclusion
        assert "two:" not in conclusion  # dependent never dispatched

    def test_unmatched_updates_are_buffered_not_dropped(self):
        import time

        bus = InMemoryBus()
        matrix = InMemoryDataMatrix(HashEmbedder())
        stop = threading.Event()

        def answer_with_noise(body):
            payload = TaskPayload.from_json(body)
            # First an unrelated update (must be buffered, not dropped, and
            # must not extend the absolute deadline), then the real one.
            for task_id in ("some-other-task", payload.task_id):
                bus.publish(
                    RESULTS_QUEUE,
                    StateUpdate(
                        trace_id=payload.trace_id,
                        task_id=task_id,
                        agent_role="critic",
                        status=UpdateStatus.VALIDATED.value,
                        summary=f"result for {task_id}",
                    ).to_json(),
                )

        listener = threading.Thread(
            target=bus.consume,
            args=(task_queue("backend_dev"), answer_with_noise, stop),
            daemon=True,
        )
        listener.start()
        orchestrator = Orchestrator(
            bus, matrix, make_config(), get_workflow("code_delivery")
        )
        orchestrator.start()
        try:
            started = time.monotonic()
            conclusion = orchestrator.run("the goal")
            assert time.monotonic() - started < 5
        finally:
            orchestrator.stop()
            stop.set()
            listener.join(timeout=2)

        assert "result for" in conclusion
        # The unrelated update is retrievable, not lost.
        assert "some-other-task" in orchestrator._unmatched
