"""
mythos/orchestration/audit.py
-----------------------------
Event-sourced audit log with deterministic replay.

Ported (agent-domain-agnostic) from the tradingboy replay engine. Every
meaningful swarm decision is appended as an immutable, sequenced event; the
JSON-Lines log is durable, and re-folding the same events through the pure
:func:`reduce_state` reducer reconstructs the exact same summary — so "replay
what my swarm did" is deterministic for incident analysis and debugging.

This is the durable counterpart to the ephemeral :class:`~mythos.orchestration.
events.EventHub` (which fans live events out to subscribers and forgets them):
the audit log persists the decision trail the blueprint's governance section
calls for. It is opt-in — pass a path (or ``MYTHOS_AUDIT_LOG``) and the
orchestrator appends to it; unset, nothing is written.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AuditEvent:
    seq: int
    kind: str
    payload: Dict[str, Any]
    ts_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AuditLog:
    """Append-only, sequenced event log that can persist to JSONL."""

    def __init__(self, path: str = "") -> None:
        self._events: List[AuditEvent] = []
        self._path = path or os.getenv("MYTHOS_AUDIT_LOG", "")
        self._lock = threading.Lock()

    def append(self, kind: str, ts_ms: int = 0, **payload: Any) -> AuditEvent:
        """Record an immutable event; persist the line if a path is configured."""
        with self._lock:
            event = AuditEvent(seq=len(self._events), kind=kind, payload=payload, ts_ms=ts_ms)
            self._events.append(event)
            if self._path:
                self._persist(event)
        return event

    def events(self) -> List[AuditEvent]:
        with self._lock:
            return list(self._events)

    def to_jsonl(self) -> str:
        with self._lock:
            return "\n".join(
                json.dumps(e.to_dict(), sort_keys=True) for e in self._events
            )

    def _persist(self, event: AuditEvent) -> None:
        # Caller holds the lock. Best-effort append; never raise into the swarm.
        try:
            directory = os.path.dirname(os.path.abspath(self._path))
            os.makedirs(directory, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        except OSError:
            pass

    @classmethod
    def from_jsonl(cls, text: str) -> "AuditLog":
        log = cls()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            log._events.append(
                AuditEvent(
                    seq=int(data["seq"]),
                    kind=str(data["kind"]),
                    payload=dict(data.get("payload", {})),
                    ts_ms=int(data.get("ts_ms", 0)),
                )
            )
        return log


def reduce_state(events: List[AuditEvent]) -> Dict[str, Any]:
    """Pure, deterministic fold of swarm events → a reconstructed summary."""
    state: Dict[str, Any] = {
        "goals_started": 0,
        "goals_completed": 0,
        "goals_failed": 0,
        "tasks_dispatched": 0,
        "tasks_validated": 0,
        "tasks_failed": 0,
        "open_tasks": 0,
        "last_goal": None,
    }
    for ev in events:
        if ev.kind == "goal.started":
            state["goals_started"] += 1
            state["last_goal"] = ev.payload.get("goal")
        elif ev.kind == "goal.completed":
            state["goals_completed"] += 1
        elif ev.kind == "goal.failed":
            state["goals_failed"] += 1
        elif ev.kind == "task.dispatched":
            state["tasks_dispatched"] += 1
            state["open_tasks"] += 1
        elif ev.kind == "task.validated":
            state["tasks_validated"] += 1
            state["open_tasks"] = max(0, state["open_tasks"] - 1)
        elif ev.kind == "task.failed":
            state["tasks_failed"] += 1
            state["open_tasks"] = max(0, state["open_tasks"] - 1)
    return state


def replay(events: List[AuditEvent]) -> Dict[str, Any]:
    """Deterministically rebuild the summary state from an event sequence."""
    return reduce_state(events)
