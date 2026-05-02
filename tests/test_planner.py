"""
tests/test_planner.py
---------------------
Unit tests for the planner module.
"""
import pytest
from mythos.planner import Plan, Planner, Task, TaskStatus


class TestTask:
    def test_initial_status(self):
        task = Task(id=0, description="do something")
        assert task.status == TaskStatus.PENDING

    def test_mark_done(self):
        task = Task(id=0, description="d")
        task.mark_done("finished!")
        assert task.status == TaskStatus.DONE
        assert task.result == "finished!"

    def test_mark_failed(self):
        task = Task(id=0, description="d")
        task.mark_failed("oops")
        assert task.status == TaskStatus.FAILED
        assert task.error == "oops"

    def test_mark_skipped(self):
        task = Task(id=0, description="d")
        task.mark_skipped()
        assert task.status == TaskStatus.SKIPPED

    def test_is_ready_no_deps(self):
        task = Task(id=0, description="d")
        assert task.is_ready(set()) is True

    def test_is_ready_with_deps(self):
        task = Task(id=1, description="d", depends_on=[0])
        assert task.is_ready(set()) is False
        assert task.is_ready({0}) is True

    def test_to_dict(self):
        task = Task(id=3, description="test")
        d = task.to_dict()
        assert d["id"] == 3
        assert d["description"] == "test"
        assert d["status"] == "pending"


class TestPlan:
    def test_create_plan(self):
        plan = Plan("write a report")
        assert plan.goal == "write a report"

    def test_add_task(self):
        plan = Plan("goal")
        t = plan.add_task("step 1")
        assert t.id == 0
        assert len(plan) == 1

    def test_next_task_returns_first_pending(self):
        plan = Plan("goal")
        t0 = plan.add_task("step 0")
        t1 = plan.add_task("step 1")
        t0.mark_done()
        next_task = plan.next_task()
        assert next_task is t1

    def test_next_task_respects_deps(self):
        plan = Plan("goal")
        t0 = plan.add_task("step 0")
        t1 = plan.add_task("step 1", depends_on=[0])
        # t1 depends on t0, which is not done yet
        assert plan.next_task() is t0
        t0.mark_done()
        assert plan.next_task() is t1

    def test_is_complete(self):
        plan = Plan("goal")
        t = plan.add_task("s")
        assert not plan.is_complete()
        t.mark_done()
        assert plan.is_complete()

    def test_has_failures(self):
        plan = Plan("goal")
        t = plan.add_task("s")
        assert not plan.has_failures()
        t.mark_failed()
        assert plan.has_failures()

    def test_progress_string(self):
        plan = Plan("goal")
        t0 = plan.add_task("s0")
        t1 = plan.add_task("s1")
        t0.mark_done()
        prog = plan.progress()
        assert "1/2" in prog

    def test_load_from_list(self):
        plan = Plan("goal")
        plan.add_task("old")
        plan.load_from_list(["new 0", "new 1", "new 2"])
        assert len(plan) == 3
        assert plan.all_tasks()[0].description == "new 0"

    def test_to_dict(self):
        plan = Plan("test goal")
        plan.add_task("t")
        d = plan.to_dict()
        assert d["goal"] == "test goal"
        assert "tasks" in d

    def test_summary(self):
        plan = Plan("my goal")
        plan.add_task("step a")
        plan.add_task("step b")
        summary = plan.summary()
        assert "my goal" in summary
        assert "step a" in summary
        assert "step b" in summary


class TestPlanner:
    def test_new_plan(self):
        planner = Planner()
        plan = planner.new_plan("do the thing")
        assert plan.goal == "do the thing"
        # Seed task added
        assert len(plan) == 1

    def test_current_plan(self):
        planner = Planner()
        assert planner.current_plan() is None
        plan = planner.new_plan("goal")
        assert planner.current_plan() is plan
