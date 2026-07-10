"""
tests/orchestration/test_posture.py
-----------------------------------
The graded health-posture ladder (ported from the tradingboy risk-posture
pattern) and its integration with the CostGovernor.
"""
from mythos.orchestration.governor import CostGovernor
from mythos.orchestration.posture import (
    Posture,
    PostureInputs,
    evaluate_posture,
)


class TestEvaluatePosture:
    def test_normal_when_healthy(self):
        d = evaluate_posture(PostureInputs())
        assert d.posture is Posture.NORMAL
        assert d.allows_new_work
        assert d.concurrency_multiplier == 1.0

    def test_reduced_at_throttle_fraction(self):
        d = evaluate_posture(PostureInputs(budget_fraction=0.65))
        assert d.posture is Posture.REDUCED
        assert d.allows_new_work
        assert d.concurrency_multiplier == 0.5

    def test_paused_at_soft_budget(self):
        d = evaluate_posture(PostureInputs(budget_fraction=0.85))
        assert d.posture is Posture.PAUSED
        assert not d.allows_new_work
        assert d.concurrency_multiplier == 0.0

    def test_halt_when_budget_exhausted(self):
        assert evaluate_posture(PostureInputs(budget_exhausted=True)).posture is Posture.HALT
        assert evaluate_posture(PostureInputs(budget_fraction=1.0)).posture is Posture.HALT

    def test_most_conservative_condition_wins(self):
        # both a throttle-level budget and a failure burst → the worse (PAUSED) wins
        d = evaluate_posture(PostureInputs(budget_fraction=0.65, error_burst=True))
        assert d.posture is Posture.PAUSED

    def test_failure_burst_pauses(self):
        d = evaluate_posture(PostureInputs(consecutive_failures=5, max_consecutive_failures=5))
        assert d.posture is Posture.PAUSED

    def test_backend_unhealthy_pauses(self):
        assert evaluate_posture(PostureInputs(backend_unhealthy=True)).posture is Posture.PAUSED

    def test_fatal_halts_over_everything(self):
        d = evaluate_posture(PostureInputs(fatal=True, budget_fraction=0.1))
        assert d.posture is Posture.HALT


class TestGovernorPosture:
    def test_normal_under_budget(self):
        gov = CostGovernor(run_token_budget=1000)
        gov.record(100)
        assert gov.posture().posture is Posture.NORMAL

    def test_reduced_then_paused_then_halt(self):
        gov = CostGovernor(run_token_budget=1000)
        gov.record(650)
        assert gov.posture().posture is Posture.REDUCED
        gov.record(200)  # 850/1000
        assert gov.posture().posture is Posture.PAUSED
        gov.record(200)  # 1050/1000
        assert gov.posture().posture is Posture.HALT

    def test_no_budget_is_always_normal(self):
        gov = CostGovernor()  # unlimited
        gov.record(10_000_000)
        assert gov.posture().posture is Posture.NORMAL

    def test_failures_feed_posture(self):
        gov = CostGovernor()
        d = gov.posture(consecutive_failures=5, max_consecutive_failures=5)
        assert d.posture is Posture.PAUSED
