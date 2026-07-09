#!/usr/bin/env python3
"""
scripts/bench.py
----------------
Micro/macro benchmarks of the swarm's in-process machinery.

Measures the framework overhead only – deterministic drivers (InMemoryBus,
InMemoryDataMatrix, HashEmbedder, StubLLM), no network, no LLM latency.  In
production the LLM calls dominate end-to-end time by 3-5 orders of magnitude;
these numbers establish that the orchestration layer itself is never the
bottleneck.  Results feed docs/PERFORMANCE.md.

    python scripts/bench.py
"""
from __future__ import annotations

import statistics
import sys
import threading
import time

sys.path.insert(0, ".")

from mythos.config import MythosConfig  # noqa: E402
from mythos.llm import LLMResponse, StubLLM  # noqa: E402
from mythos.orchestration.bus import InMemoryBus  # noqa: E402
from mythos.orchestration.config import OrchestrationConfig  # noqa: E402
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix  # noqa: E402
from mythos.orchestration.runtime import SwarmRuntime  # noqa: E402
from mythos.orchestration.schemas import MemoryNode  # noqa: E402
from mythos.orchestration.workflows import Workflow, WorkflowStep  # noqa: E402


def timed(fn, n: int) -> float:
    start = time.perf_counter()
    for _ in range(n):
        fn()
    return time.perf_counter() - start


def bench_bus(messages: int = 20_000) -> dict:
    bus = InMemoryBus()
    received = []
    stop = threading.Event()
    done = threading.Event()

    def handler(body: str) -> None:
        received.append(body)
        if len(received) == messages:
            done.set()

    consumer = threading.Thread(
        target=bus.consume, args=("q.bench", handler, stop), daemon=True
    )
    consumer.start()
    body = '{"system_instruction": "EXECUTE_SUBTASK", "n": 1}' * 4
    start = time.perf_counter()
    for _ in range(messages):
        bus.publish("q.bench", body)
    done.wait(60)
    elapsed = time.perf_counter() - start
    stop.set()
    consumer.join(timeout=2)
    return {
        "messages": messages,
        "seconds": round(elapsed, 3),
        "msgs_per_sec": int(messages / elapsed),
    }


def bench_embedder(texts: int = 5_000) -> dict:
    embedder = HashEmbedder()
    sample = "implement robust error handling for the api endpoint " * 3
    elapsed = timed(lambda: embedder.embed(sample), texts)
    return {
        "embeddings": texts,
        "seconds": round(elapsed, 3),
        "embeds_per_sec": int(texts / elapsed),
    }


def bench_matrix(nodes: int = 2_000, searches: int = 500) -> dict:
    matrix = InMemoryDataMatrix(HashEmbedder())
    upsert_start = time.perf_counter()
    for i in range(nodes):
        matrix.upsert(MemoryNode.create(
            node_type="artifact",
            content=f"artifact number {i}: fibonacci routing geocode voice test",
            source="bench",
        ))
    upsert_elapsed = time.perf_counter() - upsert_start

    latencies = []
    for _ in range(searches):
        start = time.perf_counter()
        matrix.search("fibonacci routing test", top_k=3)
        latencies.append((time.perf_counter() - start) * 1000)
    return {
        "nodes": nodes,
        "upserts_per_sec": int(nodes / upsert_elapsed),
        "search_p50_ms": round(statistics.median(latencies), 2),
        "search_p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 2),
    }


def bench_e2e(goals: int = 20) -> dict:
    """Full swarm loop per goal: dispatch -> worker -> critic -> validated."""
    workflow = Workflow(
        name="bench",
        steps=[WorkflowStep(role="backend_dev", objective_template="Echo: {goal}",
                            validation_command_template="true")],
    )

    def factory():
        return StubLLM([LLMResponse(content=None, tool_name="finish",
                                    tool_args={"conclusion": "done"})])

    runtime = SwarmRuntime(
        config=OrchestrationConfig(
            bus_backend="inmemory", matrix_backend="inmemory",
            embedder="hash", result_timeout_s=30.0, verbose=False,
        ),
        agent_config=MythosConfig(llm_provider="stub", llm_api_key="x", verbose=False),
        workflow=workflow,
        bus=InMemoryBus(),
        matrix=InMemoryDataMatrix(HashEmbedder()),
        llm_factories={"backend_dev": factory},
    )
    latencies = []
    try:
        runtime.start()
        for i in range(goals):
            start = time.perf_counter()
            runtime.run(f"goal {i}")
            latencies.append((time.perf_counter() - start) * 1000)
    finally:
        runtime.shutdown()
    return {
        "goals": goals,
        "e2e_p50_ms": round(statistics.median(latencies), 1),
        "e2e_p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 1),
    }


def main() -> None:
    print("Mythos framework benchmarks (in-memory drivers, stub LLM)\n")
    for name, fn in (
        ("message bus  ", bench_bus),
        ("hash embedder", bench_embedder),
        ("data matrix  ", bench_matrix),
        ("swarm e2e    ", bench_e2e),
    ):
        result = fn()
        print(f"{name}: " + "  ".join(f"{k}={v}" for k, v in result.items()))


if __name__ == "__main__":
    main()
