"""
mythos/orchestration/config.py
------------------------------
Configuration for the multi-agent orchestration layer.

Mirrors the ``MythosConfig`` pattern: a plain dataclass with a ``from_env``
constructor so every knob can be driven by environment variables (and by CLI
flags in ``main.py``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class OrchestrationConfig:
    """Runtime configuration for the Phase A swarm."""

    # Backends: "rabbitmq" | "inmemory" for the bus, "qdrant" | "inmemory"
    # for the data matrix.  Real services are the primary path; the in-memory
    # drivers exist for offline demos and unit tests.
    bus_backend: str = "rabbitmq"
    matrix_backend: str = "qdrant"

    # Connection settings for the real services (docker-compose defaults).
    broker_url: str = "amqp://mythos:mythos@localhost:5672/"
    qdrant_url: str = "http://localhost:6333"
    matrix_collection: str = "mythos_matrix"

    # Embedding backend: "fastembed" (local ONNX model) | "hash"
    # (deterministic feature hashing – no model download, used in tests).
    embedder: str = "fastembed"

    # Critic loop: how many attempts a subtask gets before the critic gives
    # up and reports FAILURE to the orchestrator (first run + retries).
    max_attempts: int = 3

    # Orchestrator: how long to wait for a validated result per subtask
    # before declaring the swarm stuck.
    result_timeout_s: float = 600.0

    # Logical node id stamped on every TaskPayload this orchestrator issues.
    orchestrator_id: str = "orchestrator-0"

    verbose: bool = True

    @classmethod
    def from_env(cls) -> "OrchestrationConfig":
        """Build a config from environment variables."""
        return cls(
            bus_backend=os.getenv("MYTHOS_BUS", "rabbitmq"),
            matrix_backend=os.getenv("MYTHOS_MATRIX", "qdrant"),
            broker_url=os.getenv(
                "MYTHOS_BROKER_URL", "amqp://mythos:mythos@localhost:5672/"
            ),
            qdrant_url=os.getenv("MYTHOS_QDRANT_URL", "http://localhost:6333"),
            matrix_collection=os.getenv("MYTHOS_MATRIX_COLLECTION", "mythos_matrix"),
            embedder=os.getenv("MYTHOS_EMBEDDER", "fastembed"),
            max_attempts=int(os.getenv("MYTHOS_MAX_ATTEMPTS", "3")),
            result_timeout_s=float(os.getenv("MYTHOS_RESULT_TIMEOUT_S", "600")),
            orchestrator_id=os.getenv("MYTHOS_ORCHESTRATOR_ID", "orchestrator-0"),
            verbose=os.getenv("MYTHOS_VERBOSE", "true").lower() != "false",
        )
