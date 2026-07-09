"""
mythos/orchestration/runtime.py
-------------------------------
SwarmRuntime – wires the whole Phase A swarm together.

Builds the message bus and Data Matrix from configuration, instantiates the
orchestrator, worker(s), and critic, and manages their thread lifecycle.
Every agent boundary is a real bus message even though Phase A runs all
agents as threads in one process – splitting them into separate processes or
containers later is a deployment change, not a code change.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

from ..config import MythosConfig
from ..llm import BaseLLM
from .bus import InMemoryBus, MessageBus, RabbitMQBus
from .config import OrchestrationConfig
from .critic import CriticAgent
from .matrix import (
    DataMatrix,
    HashEmbedder,
    InMemoryDataMatrix,
    QdrantDataMatrix,
    create_embedder,
)
from .orchestrator import Orchestrator
from .worker import WorkerAgent
from .workflows import Workflow, get_workflow


def create_bus(config: OrchestrationConfig) -> MessageBus:
    """Instantiate the configured message bus backend."""
    backend = config.bus_backend.lower()
    if backend == "rabbitmq":
        return RabbitMQBus(config.broker_url)
    if backend == "inmemory":
        return InMemoryBus()
    raise ValueError(f"Unknown bus backend: '{config.bus_backend}'")


def create_matrix(config: OrchestrationConfig) -> DataMatrix:
    """Instantiate the configured Data Matrix backend."""
    try:
        embedder = create_embedder(config.embedder)
    except ImportError as exc:
        # fastembed is optional; degrade to the deterministic hash embedder
        # rather than refusing to start.
        print(f"[matrix] {exc} – falling back to the hash embedder.")
        embedder = HashEmbedder()

    backend = config.matrix_backend.lower()
    if backend == "qdrant":
        return QdrantDataMatrix(
            embedder=embedder,
            url=config.qdrant_url,
            collection=config.matrix_collection,
        )
    if backend == "inmemory":
        return InMemoryDataMatrix(embedder)
    raise ValueError(f"Unknown matrix backend: '{config.matrix_backend}'")


class SwarmRuntime:
    """Owns the swarm's components and their lifecycle."""

    def __init__(
        self,
        config: Optional[OrchestrationConfig] = None,
        agent_config: Optional[MythosConfig] = None,
        workflow: Optional[Workflow] = None,
        bus: Optional[MessageBus] = None,
        matrix: Optional[DataMatrix] = None,
        llm_factories: Optional[Dict[str, Callable[[], BaseLLM]]] = None,
    ) -> None:
        self.config = config or OrchestrationConfig.from_env()
        self.agent_config = agent_config or MythosConfig.from_env()
        self.workflow = workflow or get_workflow("code_delivery")
        self.bus = bus or create_bus(self.config)
        self.matrix = matrix or create_matrix(self.config)
        factories = llm_factories or {}

        roles = {step.role for step in self.workflow.steps}
        self.workers = [
            WorkerAgent(
                role=role,
                bus=self.bus,
                matrix=self.matrix,
                config=self.config,
                agent_config=self.agent_config,
                llm_factory=factories.get(role),
            )
            for role in sorted(roles)
        ]
        self.critic = CriticAgent(
            bus=self.bus,
            matrix=self.matrix,
            config=self.config,
            agent_config=self.agent_config,
            llm_factory=factories.get("critic"),
        )
        self.orchestrator = Orchestrator(
            bus=self.bus,
            matrix=self.matrix,
            config=self.config,
            workflow=self.workflow,
        )
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all agent consumer threads."""
        if self._started:
            return
        for worker in self.workers:
            worker.start()
        self.critic.start()
        self.orchestrator.start()
        self._started = True

    def run(self, goal: str) -> str:
        """Run one goal through the swarm (starting it if necessary)."""
        self.start()
        return self.orchestrator.run(goal)

    def shutdown(self) -> None:
        """Stop all agents and release transport resources."""
        self.orchestrator.stop()
        self.critic.stop()
        for worker in self.workers:
            worker.stop()
        self.bus.close()
        self.matrix.close()
        self._started = False

    def __enter__(self) -> "SwarmRuntime":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:  # noqa: ANN002
        self.shutdown()
