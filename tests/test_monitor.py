"""
tests/test_monitor.py
---------------------
Unit tests for the self-monitoring module.
"""
from mythos.monitor import Monitor


class TestMonitorHealth:
    def test_iteration_cap_alerts(self):
        mon = Monitor(max_iterations=3)
        for _ in range(3):
            mon.record_iteration()
        health = mon.health()
        assert not health.is_healthy
        assert "iteration" in health.alert.lower()

    def test_consecutive_failures_alert(self):
        mon = Monitor(max_consecutive_failures=3)
        for _ in range(3):
            mon.record_tool_call("t", success=False)
        assert not mon.health().is_healthy

    def test_success_resets_failure_streak(self):
        mon = Monitor(max_consecutive_failures=3)
        mon.record_tool_call("t", success=False)
        mon.record_tool_call("t", success=False)
        mon.record_tool_call("t", success=True)
        assert mon.health().consecutive_failures == 0

    def test_reflection_does_not_reset_failures(self):
        # A failure streak spanning a reflection checkpoint must still trip the
        # alert (otherwise a persistently failing loop never terminates).
        mon = Monitor(max_consecutive_failures=3)
        mon.record_tool_call("t", success=False)
        mon.record_reflection("checkpoint")
        mon.record_tool_call("t", success=False)
        mon.record_tool_call("t", success=False)
        assert not mon.health().is_healthy

    def test_needs_reflection_at_interval(self):
        mon = Monitor(reflection_interval=2)
        mon.record_iteration()
        assert not mon.health().needs_reflection
        mon.record_iteration()
        assert mon.health().needs_reflection


class TestLoopDetection:
    def test_identical_calls_flagged_as_loop(self):
        mon = Monitor(loop_window=3)
        for _ in range(3):
            mon.record_tool_call("spin", success=True, signature="spin:{}")
        assert mon.health().is_looping

    def test_same_tool_different_args_not_a_loop(self):
        mon = Monitor(loop_window=3)
        for i in range(3):
            mon.record_tool_call("write_file", success=True, signature=f"write_file:{i}")
        assert not mon.health().is_looping


class TestReset:
    def test_reset_clears_all_counters(self):
        mon = Monitor(max_iterations=5)
        mon.record_iteration()
        mon.record_tool_call("t", success=False)
        mon.reset()
        health = mon.health()
        assert health.iteration == 0
        assert health.consecutive_failures == 0
        assert health.total_tool_calls == 0
        assert not health.is_looping
