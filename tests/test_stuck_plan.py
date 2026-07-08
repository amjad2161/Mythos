"""Quick check for the stuck-plan guard."""
from mythos.agent import MythosAgent
from mythos.config import MythosConfig
from mythos.llm import StubLLM
from mythos.planner import Plan


def test_stuck_plan_reports_deadlock(monkeypatch):
    agent = MythosAgent(config=MythosConfig(llm_provider="stub", verbose=False), llm=StubLLM())

    # Force a plan whose only task can never run (depends on a non-existent task).
    def fake_new_plan(goal):
        plan = Plan(goal)
        plan.add_task("blocked step", depends_on=[999])
        agent._planner._plan = plan
        return plan

    monkeypatch.setattr(agent._planner, "new_plan", fake_new_plan)
    result = agent.run("do the blocked thing")
    assert "halted" in result.lower()
    assert "unsatisfied dependencies" in result.lower()
