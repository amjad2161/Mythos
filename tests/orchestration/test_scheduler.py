"""
tests/orchestration/test_scheduler.py
-------------------------------------
The proactive routine scheduler: interval + daily-at due logic, quiet hours,
isolated firing, persistence round-trip — all with an injected clock (no real
time, no background thread needed for the logic).
"""
import time

from mythos.orchestration.scheduler import (
    Routine,
    Scheduler,
    due_routines,
    load_routines,
)

# A fixed reference instant: 2026-07-10 12:00:00 UTC
BASE = time.mktime(time.strptime("2026-07-10 12:00:00", "%Y-%m-%d %H:%M:%S")) \
    - time.timezone


class TestDueLogic:
    def test_interval_due_after_period(self):
        r = Routine(id="r1", goal="g", interval_s=60, last_fired=BASE)
        assert due_routines([r], BASE + 30) == []      # too soon
        assert due_routines([r], BASE + 60) == [r]      # exactly due
        assert due_routines([r], BASE + 120) == [r]     # overdue

    def test_disabled_never_due(self):
        r = Routine(id="r", goal="g", interval_s=1, enabled=False)
        assert due_routines([r], BASE + 10_000) == []

    def test_daily_at_fires_once_past_target(self):
        # target 09:00 UTC; last fired yesterday → due after 09:00 today
        r = Routine(id="d", goal="brief", daily_at="09:00", last_fired=BASE - 86400)
        # 08:00 today — before target
        before = due_routines([r], BASE - 4 * 3600)
        assert before == []
        # 12:00 today — past target, not yet fired today → due
        assert due_routines([r], BASE) == [r]

    def test_daily_at_not_refired_same_day(self):
        r = Routine(id="d", goal="brief", daily_at="09:00", last_fired=BASE - 2 * 3600)
        # already fired at 10:00 today; 12:00 → not due again
        assert due_routines([r], BASE) == []


class TestQuietHours:
    def test_quiet_window_suppresses(self):
        # quiet 22:00–06:00 (wrap). An interval routine at 23:00 is suppressed.
        r = Routine(id="q", goal="g", interval_s=1, quiet_start=22, quiet_end=6,
                    last_fired=0)
        at_2300 = BASE + 11 * 3600  # 23:00 UTC
        assert due_routines([r], at_2300) == []
        at_1200 = BASE               # 12:00 UTC — outside quiet
        assert due_routines([r], at_1200) == [r]

    def test_in_quiet_hours_helper(self):
        r = Routine(id="q", goal="g", quiet_start=22, quiet_end=6)
        assert r.in_quiet_hours(23)
        assert r.in_quiet_hours(3)
        assert not r.in_quiet_hours(12)
        day = Routine(id="d", goal="g", quiet_start=9, quiet_end=17)
        assert day.in_quiet_hours(12)
        assert not day.in_quiet_hours(20)


class TestScheduler:
    def test_tick_fires_due_and_stamps(self):
        fired = []
        now = [BASE]
        sched = Scheduler(fire=fired.append, clock=lambda: now[0])
        sched.add(Routine(id="r", goal="do it", interval_s=60, last_fired=BASE - 100))
        got = sched.tick()
        assert [r.id for r in got] == ["r"]
        assert [r.goal for r in fired] == ["do it"]
        # last_fired advanced, so a second immediate tick does nothing
        assert sched.tick() == []

    def test_one_bad_routine_does_not_stop_others(self):
        calls = []

        def fire(routine):
            calls.append(routine.id)
            if routine.id == "bad":
                raise RuntimeError("boom")

        sched = Scheduler(fire=fire, clock=lambda: BASE)
        sched.add(Routine(id="bad", goal="x", interval_s=1, last_fired=0))
        sched.add(Routine(id="good", goal="y", interval_s=1, last_fired=0))
        fired = sched.tick()
        assert set(calls) == {"bad", "good"}       # both attempted
        assert {r.id for r in fired} == {"good"}    # only the successful one reported

    def test_add_remove_list(self):
        sched = Scheduler(fire=lambda r: None, clock=lambda: BASE)
        sched.add(Routine(id="a", goal="g"))
        assert [r.id for r in sched.list()] == ["a"]
        assert sched.remove("a")
        assert sched.list() == []
        assert not sched.remove("a")

    def test_persistence_roundtrip(self, tmp_path):
        path = str(tmp_path / "routines.json")
        s1 = Scheduler(fire=lambda r: None, clock=lambda: BASE, path=path)
        s1.add(Routine(id="r", goal="daily brief", daily_at="07:00", name="Brief"))
        # a fresh scheduler pointed at the same file reloads the routine
        s2 = Scheduler(fire=lambda r: None, clock=lambda: BASE, path=path)
        loaded = s2.list()
        assert len(loaded) == 1
        assert loaded[0].goal == "daily brief"
        assert loaded[0].daily_at == "07:00"

    def test_load_routines_helper(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text('[{"id":"x","goal":"g","interval_s":30}]', encoding="utf-8")
        routines = load_routines(str(path))
        assert routines[0].id == "x"
        assert routines[0].interval_s == 30

    def test_load_routines_missing_file(self, tmp_path):
        assert load_routines(str(tmp_path / "nope.json")) == []
