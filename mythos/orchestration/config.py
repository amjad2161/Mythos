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

    # LLM call resilience: exponential-backoff retries on transient errors.
    llm_retry_attempts: int = 3
    llm_retry_base_s: float = 1.0

    # Cost governance (token budgets; 0 = unlimited).
    hourly_token_budget: int = 0
    run_token_budget: int = 0

    # Persona overrides directory ('' = packaged personas only).
    persona_dir: str = ""

    # Dynamic orchestration (Phase B): LLM-driven goal decomposition.
    dynamic: bool = False
    decomposer_model: str = "claude-haiku-4-5"
    decomposer_max_steps: int = 6
    fallback_workflow: str = "code_delivery"

    # Orchestrator: how long to wait for a validated result per subtask
    # before declaring the swarm stuck.  0 = auto: derived from the subtask's
    # constraints so the window always covers the permitted retry budget
    # (max_attempts x (timeout_ms + validation slack)).
    result_timeout_s: float = 0.0

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
            llm_retry_attempts=int(os.getenv("MYTHOS_LLM_RETRY_ATTEMPTS", "3")),
            llm_retry_base_s=float(os.getenv("MYTHOS_LLM_RETRY_BASE_S", "1.0")),
            hourly_token_budget=int(os.getenv("MYTHOS_HOURLY_TOKEN_BUDGET", "0")),
            run_token_budget=int(os.getenv("MYTHOS_RUN_TOKEN_BUDGET", "0")),
            persona_dir=os.getenv("MYTHOS_PERSONA_DIR", ""),
            dynamic=os.getenv("MYTHOS_DYNAMIC", "false").lower() == "true",
            decomposer_model=os.getenv("MYTHOS_DECOMPOSER_MODEL", "claude-haiku-4-5"),
            decomposer_max_steps=int(os.getenv("MYTHOS_DECOMPOSER_MAX_STEPS", "6")),
            fallback_workflow=os.getenv("MYTHOS_FALLBACK_WORKFLOW", "code_delivery"),
            result_timeout_s=float(os.getenv("MYTHOS_RESULT_TIMEOUT_S", "0")),
            orchestrator_id=os.getenv("MYTHOS_ORCHESTRATOR_ID", "orchestrator-0"),
            verbose=os.getenv("MYTHOS_VERBOSE", "true").lower() != "false",
        )
