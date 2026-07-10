"""
tests/test_monitor_budget.py
----------------------------
Token-budget and wall-deadline enforcement in the Monitor, and the
executor's usage recording.
"""
from mythos.agent import MythosAgent
from mythos.config import MythosConfig
from mythos.llm import LLMResponse, StubLLM
from mythos.monitor import Monitor


class TestTokenAccounting:
    def test_record_usage_accumulates_per_kind(self):
        monitor = Monitor()
        monitor.record_usage({"input": 10, "output": 5})
        monitor.record_usage({"input": 1, "cache_read": 100})
        assert monitor.token_usage == {
            "input": 11, "output": 5, "cache_read": 100, "cache_creation": 0,
        }
        assert monitor.total_tokens == 116

    def test_budget_alert_trips_at_limit(self):
        monitor = Monitor(max_total_tokens=100)
        monitor.record_usage({"input": 60, "output": 39})
        assert monitor.health().is_healthy
        monitor.record_usage({"output": 1})
        health = monitor.health()
        assert not health.is_healthy
        assert "Token budget exhausted" in health.alert

    def test_zero_budget_means_unlimited(self):
        monitor = Monitor(max_total_tokens=0)
        monitor.record_usage({"input": 10 ** 9})
        assert monitor.health().is_healthy

    def test_reset_clears_usage_and_deadline(self):
        monitor = Monitor(max_total_tokens=10)
        monitor.record_usage({"input": 50})
        monitor.reset()
        assert monitor.total_tokens == 0
        assert monitor.health().is_healthy


class TestWallDeadline:
    def test_deadline_alert(self, monkeypatch):
        import time as time_module

        clock = {"now": 1000.0}
        monkeypatch.setattr(time_module, "monotonic", lambda: clock["now"])
        monitor = Monitor(max_wall_seconds=30)
        monitor.reset()  # stamps the (patched) start time
        assert monitor.health().is_healthy
        clock["now"] += 31
        health = monitor.health()
        assert not health.is_healthy
        assert "deadline" in health.alert.lower()


class TestExecutorRecordsUsage:
    def test_agent_run_stops_on_token_budget(self):
        class TokenHungryStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                return LLMResponse(
                    content="still working...",
                    usage={"input": 400, "output": 200},
                )

        config = MythosConfig(
            llm_provider="stub", llm_api_key="unused", verbose=False,
            max_total_tokens=1000, max_iterations=50,
        )
        agent = MythosAgent(config=config, llm=TokenHungryStub())
        conclusion = agent.run("goal")
        assert not agent.last_run_ok
        assert "Token budget exhausted" in (agent.last_halt_reason or "") or \
               "Token budget exhausted" in conclusion
        # 600 tokens/call -> the budget trips on the 2nd call's health check.
        assert agent.monitor.total_tokens >= 1000
