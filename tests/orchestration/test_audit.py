"""
tests/orchestration/test_audit.py
---------------------------------
Event-sourced audit log: append, deterministic replay, JSONL round-trip,
opt-in file persistence, and the EventHub tee.
"""
from mythos.orchestration.audit import AuditLog, reduce_state, replay
from mythos.orchestration.events import EventHub


class TestAuditLog:
    def test_append_sequences_events(self):
        log = AuditLog()
        e0 = log.append("goal.started", goal="do X")
        e1 = log.append("task.dispatched", task_id="t1")
        assert (e0.seq, e1.seq) == (0, 1)
        assert e0.payload["goal"] == "do X"

    def test_reduce_state_folds_lifecycle(self):
        log = AuditLog()
        log.append("goal.started", goal="g")
        log.append("task.dispatched", task_id="t1")
        log.append("task.dispatched", task_id="t2")
        log.append("task.validated", task_id="t1")
        log.append("task.failed", task_id="t2")
        log.append("goal.completed")
        state = reduce_state(log.events())
        assert state["goals_started"] == 1
        assert state["goals_completed"] == 1
        assert state["tasks_dispatched"] == 2
        assert state["tasks_validated"] == 1
        assert state["tasks_failed"] == 1
        assert state["open_tasks"] == 0
        assert state["last_goal"] == "g"

    def test_replay_is_deterministic_across_roundtrip(self):
        log = AuditLog()
        for i in range(3):
            log.append("task.dispatched", task_id=f"t{i}")
        log.append("task.validated", task_id="t0")
        jsonl = log.to_jsonl()
        restored = AuditLog.from_jsonl(jsonl)
        assert replay(restored.events()) == replay(log.events())
        assert restored.to_jsonl() == jsonl

    def test_file_persistence(self, tmp_path):
        path = tmp_path / "sub" / "audit.jsonl"
        log = AuditLog(path=str(path))
        log.append("goal.started", goal="persisted")
        log.append("goal.completed")
        # each append writes one JSONL line
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        reloaded = AuditLog.from_jsonl(path.read_text(encoding="utf-8"))
        assert reduce_state(reloaded.events())["goals_started"] == 1

    def test_no_path_writes_nothing(self, tmp_path):
        log = AuditLog()  # no path
        log.append("goal.started")
        assert not list(tmp_path.iterdir())


class TestEventHubTee:
    def test_hub_tees_emits_to_audit(self):
        audit = AuditLog()
        hub = EventHub(audit=audit)
        hub.emit("goal.started", trace_id="tr1", goal="g")
        hub.emit("task.dispatched", trace_id="tr1", task_id="t1", role="backend_dev")
        events = audit.events()
        assert [e.kind for e in events] == ["goal.started", "task.dispatched"]
        # hub-level fields are captured in the audit payload
        assert events[0].payload["trace_id"] == "tr1"
        assert events[0].payload["goal"] == "g"
        assert events[1].payload["role"] == "backend_dev"
        assert events[0].ts_ms > 0

    def test_hub_without_audit_is_unaffected(self):
        hub = EventHub()  # no audit sink
        ev = hub.emit("goal.started", trace_id="x")
        assert ev.kind == "goal.started"
