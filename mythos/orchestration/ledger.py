"""
mythos/orchestration/ledger.py
------------------------------
TaskLedger – externalized, durable progress for a goal.

The pattern comes from long-running autonomous-coding agents: keep the source
of truth about progress *outside* any context window (a feature-list document
updated step by step), so agents can reset context between steps without
losing state, and humans/tools can observe progress at any time.

The ledger is a single ``MemoryNode`` (``node_type="ledger"``) whose content
is a JSON document with one entry per workflow step.  The node id is stable
across updates – each ``mark_*`` call re-upserts the same node.

Single-writer by design: only the orchestrator mutates the ledger (neither
matrix driver offers compare-and-swap, so concurrent read-modify-write from
workers would lose updates).  Workers' attempt counts still arrive via the
terminal ``StateUpdate.attempt``.
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional

from .matrix import DataMatrix
from .schemas import MemoryNode, SchemaError, TRUST_AGENT


class TaskLedger:
    """Durable per-goal progress document stored in the Data Matrix."""

    def __init__(self, matrix: DataMatrix) -> None:
        self._matrix = matrix

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create(
        self,
        trace_id: str,
        goal: str,
        steps: List[Dict[str, str]],
        goal_node_id: Optional[str] = None,
    ) -> str:
        """
        Create the ledger for a goal; *steps* is ``[{"role", "objective"}]``.
        Returns the stable ledger node id.
        """
        document = {
            "trace_id": trace_id,
            "goal": goal,
            "created_at": _utcnow(),
            "steps": [
                {
                    "index": i,
                    "role": step["role"],
                    "objective": step["objective"],
                    "task_id": "",
                    "status": "pending",
                    "attempts": 0,
                    "summary": "",
                }
                for i, step in enumerate(steps)
            ],
        }
        edges = (
            [{"relation": "tracks", "target_id": goal_node_id}] if goal_node_id else []
        )
        node = MemoryNode.create(
            node_type="ledger",
            content=json.dumps(document, ensure_ascii=False),
            source="orchestrator",
            trust_score=TRUST_AGENT,
            edges=edges,
        )
        self._matrix.upsert(node)
        return node.node_id

    # ------------------------------------------------------------------
    # Updates (single writer: the orchestrator)
    # ------------------------------------------------------------------

    def mark_dispatched(self, ledger_id: str, index: int, task_id: str) -> None:
        self._mutate(ledger_id, index, task_id=task_id, status="dispatched")

    def mark_terminal(
        self,
        ledger_id: str,
        index: int,
        status: str,
        attempts: int,
        summary: str,
    ) -> None:
        self._mutate(
            ledger_id, index, status=status, attempts=attempts, summary=summary[:500]
        )

    def read(self, ledger_id: str) -> Dict[str, Any]:
        """Return the parsed ledger document; ``SchemaError`` when corrupt."""
        nodes = self._matrix.get([ledger_id])
        if not nodes:
            raise SchemaError(f"ledger node {ledger_id} not found")
        try:
            document = json.loads(nodes[0].content)
        except ValueError as exc:
            raise SchemaError(f"corrupt ledger content: {exc}") from exc
        if not isinstance(document, dict) or "steps" not in document:
            raise SchemaError("corrupt ledger content: not a ledger document")
        return document

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _mutate(self, ledger_id: str, index: int, **updates: Any) -> None:
        nodes = self._matrix.get([ledger_id])
        if not nodes:
            raise SchemaError(f"ledger node {ledger_id} not found")
        node = nodes[0]
        document = self.read(ledger_id)
        steps = document["steps"]
        if not 0 <= index < len(steps):
            raise SchemaError(f"ledger step index {index} out of range")
        steps[index].update(updates)
        document["updated_at"] = _utcnow()
        node.content = json.dumps(document, ensure_ascii=False)
        self._matrix.upsert(node)  # same node_id -> stable document


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
