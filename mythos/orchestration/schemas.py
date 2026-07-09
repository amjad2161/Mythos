"""
mythos/orchestration/schemas.py
-------------------------------
Machine-to-machine (M2M) message schemas for the multi-agent system.

Agents never exchange free natural-language text.  Every message on the bus is
one of two strict JSON envelopes:

* ``TaskPayload``  – a work order issued by the orchestrator (or re-issued by
  the critic on retry) to a worker agent.
* ``StateUpdate``  – a structured result object emitted by a worker or critic.

Long-term knowledge lives in the Data Matrix as ``MemoryNode`` records –
verbatim content plus embedding, trust metadata, and typed graph edges.

Deserialisation is strict: unknown enum values or missing required fields
raise ``SchemaError`` instead of producing half-valid objects, so a malformed
message can never propagate silently through the swarm.
"""
from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SchemaError(ValueError):
    """Raised when a message cannot be parsed into a valid schema object."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SystemInstruction(str, Enum):
    """Command verbs understood by worker agents."""

    EXECUTE_SUBTASK = "EXECUTE_SUBTASK"
    RETRY_SUBTASK = "RETRY_SUBTASK"


class UpdateStatus(str, Enum):
    """Lifecycle states carried by a StateUpdate."""

    SUCCESS = "SUCCESS"      # worker believes it completed the subtask
    FAILURE = "FAILURE"      # terminal failure (worker crash or retries exhausted)
    RETRY = "RETRY"          # critic sent the subtask back to the worker
    VALIDATED = "VALIDATED"  # critic verified the result; safe to bubble up


# ---------------------------------------------------------------------------
# TaskPayload – the work order envelope
# ---------------------------------------------------------------------------

@dataclass
class TargetAgent:
    """Addressing block: which agent role should pick this task up."""

    role: str                       # e.g. "backend_dev", "critic"
    access_level: str = "standard"  # reserved for Phase B permission tiers


@dataclass
class TaskParameters:
    """What the worker must achieve."""

    objective: str
    # Node ids in the Data Matrix that hold the task's context.  Pointers are
    # sent instead of full text so payloads stay small and content verbatim.
    context_pointers: List[str] = field(default_factory=list)
    language: str = "en"
    # Optional deterministic check the critic runs to validate the result
    # (a shell command; exit code 0 = pass).  Empty = critic uses LLM judgment.
    validation_command: str = ""
    success_criteria: str = ""


@dataclass
class Constraints:
    """Hard execution limits enforced by the worker runtime."""

    max_compute_tokens: int = 100_000
    # Tool names stripped from the worker's registry for this task.
    forbidden_modules: List[str] = field(default_factory=list)
    timeout_ms: int = 300_000


@dataclass
class TaskPayload:
    """The M2M work-order envelope routed over the message bus."""

    system_instruction: str          # SystemInstruction value
    trace_id: str                    # constant across one whole goal
    task_id: str                     # unique per subtask
    orchestrator_node: str           # logical id of the issuing orchestrator
    target_agent: TargetAgent
    task_parameters: TaskParameters
    constraints: Constraints = field(default_factory=Constraints)
    # Queue the worker publishes its StateUpdate to.  The vision names this
    # `callback_webhook`; Phase A transports callbacks over AMQP queues (an
    # HTTP webhook adapter is a Phase B item – see docs/ARCHITECTURE.md).
    callback_queue: str = ""
    attempt: int = 1
    # Verbatim failure output injected by the critic on RETRY_SUBTASK.
    error_log: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "TaskPayload":
        data = _parse_object(raw, "TaskPayload")
        try:
            instruction = SystemInstruction(data["system_instruction"]).value
            return cls(
                system_instruction=instruction,
                trace_id=_require_str(data, "trace_id"),
                task_id=_require_str(data, "task_id"),
                orchestrator_node=_require_str(data, "orchestrator_node"),
                target_agent=TargetAgent(**_require_dict(data, "target_agent")),
                task_parameters=TaskParameters(**_require_dict(data, "task_parameters")),
                constraints=Constraints(**data.get("constraints") or {}),
                callback_queue=str(data.get("callback_queue", "")),
                attempt=int(data.get("attempt", 1)),
                error_log=data.get("error_log"),
            )
        except SchemaError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError(f"Invalid TaskPayload: {exc}") from exc


# ---------------------------------------------------------------------------
# StateUpdate – the structured result envelope
# ---------------------------------------------------------------------------

@dataclass
class StateUpdate:
    """Structured result object – never free conversational text."""

    trace_id: str
    task_id: str
    agent_role: str
    status: str                      # UpdateStatus value
    # Data Matrix node ids holding the produced artifacts/observations.
    result_pointers: List[str] = field(default_factory=list)
    # Short machine-loggable line (one sentence), not a conversation.
    summary: str = ""
    # Verbatim stderr / stack trace / failing check output on FAILURE.
    error_log: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    attempt: int = 1
    # Round-tripped work order so the critic can autonomously re-dispatch the
    # subtask (RETRY) without involving the orchestrator.
    task_payload: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "StateUpdate":
        data = _parse_object(raw, "StateUpdate")
        try:
            status = UpdateStatus(data["status"]).value
            return cls(
                trace_id=_require_str(data, "trace_id"),
                task_id=_require_str(data, "task_id"),
                agent_role=_require_str(data, "agent_role"),
                status=status,
                result_pointers=list(data.get("result_pointers") or []),
                summary=str(data.get("summary", "")),
                error_log=data.get("error_log"),
                metrics=dict(data.get("metrics") or {}),
                attempt=int(data.get("attempt", 1)),
                task_payload=data.get("task_payload"),
            )
        except SchemaError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError(f"Invalid StateUpdate: {exc}") from exc

    def payload(self) -> Optional[TaskPayload]:
        """Reconstruct the originating TaskPayload, if it was carried along."""
        if self.task_payload is None:
            return None
        return TaskPayload.from_json(json.dumps(self.task_payload))


# ---------------------------------------------------------------------------
# MemoryNode – the Data Matrix ground-truth record
# ---------------------------------------------------------------------------

# Trust scale: system instructions are absolute ground truth and outrank any
# conflicting information during context fusion.
TRUST_SYSTEM = 1.0
TRUST_USER = 0.9
TRUST_AGENT = 0.6


@dataclass
class MemoryNode:
    """One item of ground truth in the Data Matrix (vector + graph hybrid)."""

    node_id: str
    node_type: str                   # "system_instruction" | "goal" | "artifact" | ...
    content: str                     # VERBATIM raw content – never paraphrased
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Typed knowledge-graph edges: [{"relation": "...", "target_id": "..."}]
    edges: List[Dict[str, str]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        node_type: str,
        content: str,
        source: str,
        trust_score: float = TRUST_AGENT,
        verbatim_required: bool = False,
        edges: Optional[List[Dict[str, str]]] = None,
    ) -> "MemoryNode":
        """Build a node with a fresh id and standard metadata."""
        return cls(
            node_id=str(uuid.uuid4()),
            node_type=node_type,
            content=content,
            metadata={
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": source,
                "trust_score": trust_score,
                "verbatim_required": verbatim_required,
            },
            edges=list(edges or []),
        )

    @property
    def trust_score(self) -> float:
        return float(self.metadata.get("trust_score", TRUST_AGENT))

    @property
    def verbatim_required(self) -> bool:
        return bool(self.metadata.get("verbatim_required", False))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryNode":
        try:
            return cls(
                node_id=_require_str(data, "node_id"),
                node_type=_require_str(data, "node_type"),
                content=str(data.get("content", "")),
                metadata=dict(data.get("metadata") or {}),
                edges=[dict(e) for e in (data.get("edges") or [])],
            )
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"Invalid MemoryNode: {exc}") from exc


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_object(raw: str, kind: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{kind} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError(f"{kind} must be a JSON object, got {type(data).__name__}")
    return data


def _require_str(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise SchemaError(f"missing or empty required field '{key}'")
    return value


def _require_dict(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise SchemaError(f"missing or invalid required object '{key}'")
    return value


def new_trace_id() -> str:
    return f"trace_{uuid.uuid4().hex[:12]}"


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"
