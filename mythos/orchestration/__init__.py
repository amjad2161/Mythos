"""
mythos.orchestration
--------------------
Phase A of the Mythos multi-agent system: deterministic orchestration.

This subpackage layers a three-tier multi-agent architecture on top of the
single-agent core (see ``docs/ARCHITECTURE.md``):

1. **Orchestrator ("Agent Boss")** – decomposes a goal into a task matrix and
   routes each subtask to a specialised agent.  It never executes work itself.
2. **Data Matrix** – shared long-term memory: a vector store for semantic
   search fused with a knowledge graph (typed edges between nodes).
3. **Agentic swarm** – worker and critic agents that communicate exclusively
   through structured JSON payloads over a message bus (never free text).

Production infrastructure is RabbitMQ (message bus) and Qdrant (data matrix);
in-memory drivers behind the same interfaces support offline runs and tests.

Optional dependencies (``pip install mythos[orchestration]``) are imported
lazily – importing this package never requires pika/qdrant/fastembed.
"""

from .config import OrchestrationConfig
from .schemas import (
    Constraints,
    MemoryNode,
    SchemaError,
    StateUpdate,
    TargetAgent,
    TaskParameters,
    TaskPayload,
    UpdateStatus,
)

__all__ = [
    "OrchestrationConfig",
    "Constraints",
    "MemoryNode",
    "SchemaError",
    "StateUpdate",
    "TargetAgent",
    "TaskParameters",
    "TaskPayload",
    "UpdateStatus",
    "SwarmRuntime",
    "Persona",
    "TaskLedger",
    "CostGovernor",
    "DynamicDecomposer",
    "ingest_taxonomy",
    "parse_taxonomy",
    "IngestResult",
]

# Heavier members pull in the agent stack / optional deps; kept lazy so that
# `import mythos.orchestration` stays cheap for schema-only consumers.
_LAZY = {
    "SwarmRuntime": ("runtime", "SwarmRuntime"),
    "Persona": ("personas", "Persona"),
    "TaskLedger": ("ledger", "TaskLedger"),
    "CostGovernor": ("governor", "CostGovernor"),
    "DynamicDecomposer": ("decomposer", "DynamicDecomposer"),
    "ingest_taxonomy": ("ingest", "ingest_taxonomy"),
    "parse_taxonomy": ("ingest", "parse_taxonomy"),
    "IngestResult": ("ingest", "IngestResult"),
}


def __getattr__(name):  # noqa: ANN001, ANN202
    target = _LAZY.get(name)
    if target is not None:
        import importlib  # noqa: PLC0415

        module = importlib.import_module(f".{target[0]}", __name__)
        return getattr(module, target[1])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
