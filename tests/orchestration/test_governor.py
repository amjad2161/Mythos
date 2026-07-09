"""
tests/orchestration/test_governor.py
------------------------------------
CostGovernor budgets and the worker's refuse-when-tripped behaviour.
"""
from mythos.orchestration.bus import InMemoryBus
from mythos.orchestration.governor import CostGovernor
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.schemas import UpdateStatus
from mythos.orchestration.worker import WorkerAgent

from .conftest import make_agent_config, make_orch_config, make_payload


class TestBudgets:
    def test_unlimited_by_default(self):
        governor = CostGovernor()
        governor.record(10 ** 9)
        assert governor.check() is None

    def test_run_budget_trips(self):
        governor = CostGovernor(run_token_budget=100)
        governor.record(60)
        assert governor.check() is None
        governor.record(40)
        reason = governor.check()
        assert reason is not None
        assert "run token budget" in reason

    def test_reset_run_clears_run_total_only(self):
        governor = CostGovernor(run_token_budget=100, hourly_token_budget=150)
        governor.record(120)
        assert "run token budget" in governor.check()
        governor.reset_run()
        assert governor.check() is None          # run counter cleared
        governor.record(40)
        assert "hourly token budget" in governor.check()  # window still counts

    def test_hourly_window_prunes_old_events(self, monkeypatch):
        import mythos.orchestration.governor as governor_module

        clock = {"now": 1000.0}
        monkeypatch.setattr(governor_module.time, "monotonic", lambda: clock["now"])
        governor = CostGovernor(hourly_token_budget=100)
        governor.record(90)
        clock["now"] += 3601  # the event ages out of the window
        governor.record(50)
        assert governor.check() is None
        assert governor.window_total == 50


class TestWorkerRefusal:
    def test_tripped_governor_refuses_without_llm_call(self):
        factory_calls = []

        def factory():
            factory_calls.append(1)
            raise AssertionError("LLM must not be constructed for refused tasks")

        governor = CostGovernor(run_token_budget=1)
        governor.record(5)
        worker = WorkerAgent(
            role="backend_dev",
            bus=InMemoryBus(),
            matrix=InMemoryDataMatrix(HashEmbedder()),
            config=make_orch_config(),
            agent_config=make_agent_config(),
            llm_factory=factory,
            governor=governor,
        )
        update = worker.handle(make_payload())
        assert update.status == UpdateStatus.FAILURE.value
        assert "COST_GOVERNOR_TRIPPED" in update.error_log
        assert factory_calls == []
