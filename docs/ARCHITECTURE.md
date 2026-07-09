# Mythos Multi-Agent Architecture

This document maps the full multi-agent vision onto the Mythos codebase: the
three-layer logical architecture, the machine-to-machine (M2M) protocols, the
Data Matrix, and the three-phase evolution roadmap. **Phase A is implemented**
in `mythos/orchestration/`; Phases B and C are designed here and deferred.

## 1. Vision

Mythos evolves from a single autonomous agent into a living digital work
environment: a proactive, always-available digital workforce. The system is a
network of distributed cognitions under central management, aiming at
autonomous execution of system-level engineering — from abstract planning
through code, debugging, and final delivery — with the human acting as the
top-level approver ("Agent Boss") rather than an operator.

Three principles govern every layer:

1. **No free text between machines.** Agents exchange strict JSON envelopes
   over an asynchronous message bus. Natural language exists only inside an
   agent (its LLM reasoning) and at the human boundary.
2. **One ground truth.** All durable knowledge lives in the Data Matrix —
   verbatim content, trust-scored, semantically indexed, and graph-linked.
   Agents navigate it autonomously instead of being spoon-fed context.
3. **Structural quality gates.** Workers never report directly to the
   orchestrator. Every result is intercepted by a critic, and failed work
   loops back to the worker autonomously until validated or exhausted.

## 2. The three layers

```
                          ┌────────────────────────────┐
   user intent ──────────▶│  LAYER 1: Orchestrator     │  mythos/orchestration/orchestrator.py
                          │  ("Agent Boss")            │  workflows.py
                          │  decompose → route → track │
                          └──────────┬─────────────────┘
                                     │ TaskPayload (JSON)          ▲ StateUpdate: VALIDATED / FAILURE
                                     ▼                             │
                          ┌────────────────────────────┐           │
                          │  message bus (RabbitMQ)    │  bus.py   │
                          │  q.tasks.<role>            │───────────┘ q.orchestrator.results
                          └──────────┬─────────────────┘
                                     ▼
        ┌───────────────────────────────────────────────────┐
        │  LAYER 3: Agentic swarm                           │
        │  WorkerAgent(role) ── StateUpdate ──▶ CriticAgent │  worker.py / critic.py
        │        ▲                                  │       │
        │        └───── TaskPayload: RETRY ─────────┘       │  (autonomous debug loop)
        └──────────────────────┬────────────────────────────┘
                               │ navigate / upsert
                               ▼
                  ┌────────────────────────────┐
                  │  LAYER 2: Data Matrix      │  matrix.py
                  │  Qdrant: vectors + payload │
                  │  (embeddings ∪ knowledge   │
                  │   graph in one store)      │
                  └────────────────────────────┘
```

### Layer 1 — Centralized Orchestrator (`orchestrator.py`)

The orchestrator receives an abstract goal and **never executes work**. It:

1. Seeds the Data Matrix with a `system_instruction` node (trust 1.0,
   verbatim) and a `goal` node (edge: `goal --refines--> system_instruction`).
2. Decomposes the goal into a task matrix. In Phase A decomposition is a
   rigid, named `Workflow` (`workflows.py`) mapped onto the existing
   single-agent `Plan`/`Task` structures (`mythos/planner.py`), so dependency
   tracking and deadlock detection are reused, not reinvented.
3. Dispatches each ready subtask as a `TaskPayload` to `q.tasks.<role>` and
   blocks on `q.orchestrator.results` for its terminal `StateUpdate`.
4. Marks the plan task done/failed and continues until the plan completes.

### Layer 2 — Data Matrix (`matrix.py`)

A hybrid vector + knowledge-graph memory in a single Qdrant collection.
Each `MemoryNode` is one point:

| Vision field         | Implementation                                        |
|----------------------|-------------------------------------------------------|
| `node_id`            | Qdrant point id (UUID)                                 |
| `embedding_vector`   | the point vector, computed from `content` at upsert    |
| `content` (verbatim) | payload field — never paraphrased                      |
| `metadata`           | payload: timestamp, source, `trust_score`, `verbatim_required` |
| `edges`              | payload: `[{"relation", "target_id"}]` — the knowledge graph |

**Autonomous navigation** (`DataMatrix.navigate`) implements the traversal
algorithm from the vision:

1. *Semantic query* — the agent's need is embedded and KNN-searched
   (plus any explicit `context_pointers` from the payload).
2. *Graph traversal* — each hit's edges are followed (1 hop in Phase A) to
   pull adjacent, necessary context.
3. *Data fusion* — results are deduplicated and **sorted by trust score**;
   `fuse_context` renders them into the worker's context window, with
   `verbatim_required` content reproduced exactly inside delimiters. System
   instructions (trust 1.0) therefore always appear first and outrank
   conflicting lower-trust content.

Embeddings: Anthropic has no embeddings endpoint, so the default embedder is
**fastembed** (`BAAI/bge-small-en-v1.5`, 384-d, local ONNX — no key, no cost).
A deterministic `HashEmbedder` (feature hashing at the same dimensionality)
backs tests and offline runs. Trust-conflict handling in Phase A is ordering
only; semantic contradiction detection is Phase B.

### Layer 3 — Agentic swarm (`worker.py`, `critic.py`, `roles.py`)

Each `WorkerAgent` wraps the **existing single-agent core** — `MythosAgent` +
`Executor` — as its execution engine, giving it a bus lifecycle. Per payload:

1. `matrix.navigate(objective, seed_ids=context_pointers)` → fused context.
2. A per-role Tools API (`roles.py`) is built by filtering the single-agent
   registry, minus the payload's `constraints.forbidden_modules`.
3. Constraints map onto the existing `Monitor`: `max_compute_tokens` derives
   an iteration cap (`budget // llm_max_tokens`), `timeout_ms` is enforced
   as a deadline.
4. The produced result is upserted into the matrix as an `artifact` node
   (edges: `produced_for → context nodes`) and a `StateUpdate` carrying
   *pointers*, not text, goes to the critic queue.

The `CriticAgent` is the structural quality gate. Its Tools API is
read/execute only — **it verifies, it never fixes**. Validation is mechanical
first (the payload's `validation_command`, exit 0 = pass) with LLM judgment
as fallback (verdict protocol: conclusion starts with `VERDICT: PASS` or
`VERDICT: FAIL: <reason>`; no explicit verdict fails safe). On failure the
critic injects the **exact, verbatim failure output** into `error_log`, bumps
`attempt`, and re-publishes the payload as `RETRY_SUBTASK` straight back to
the worker queue — the orchestrator and the user are not involved. Only after
validation (or retry exhaustion) does a result reach
`q.orchestrator.results`.

## 3. M2M protocol

Transport: RabbitMQ (AMQP, `pika`), durable queues on the default exchange,
manual acks, redeliver-once-then-drop on handler crash. An `InMemoryBus`
implements the identical contract for offline runs. Queue topology:

```
q.tasks.<role>           work orders  (orchestrator → worker, critic → worker on retry)
q.critic.review          worker results (worker → critic; structurally unavoidable)
q.orchestrator.results   validated / terminal results (critic → orchestrator)
```

### TaskPayload (work order)

```json
{
  "system_instruction": "EXECUTE_SUBTASK",
  "trace_id": "trace_8847aa01b2c3",
  "task_id": "task_0f1e2d3c4b5a",
  "orchestrator_node": "orchestrator-0",
  "target_agent": { "role": "backend_dev", "access_level": "standard" },
  "task_parameters": {
    "objective": "Implement robust error handling for /v1/matrix/nav",
    "context_pointers": ["<goal-node-uuid>", "<spec-node-uuid>"],
    "language": "en",
    "validation_command": "python -m pytest tests/api -q",
    "success_criteria": "All endpoint tests pass"
  },
  "constraints": {
    "max_compute_tokens": 100000,
    "forbidden_modules": ["run_shell"],
    "timeout_ms": 300000
  },
  "callback_queue": "q.critic.review",
  "attempt": 1,
  "error_log": null
}
```

`system_instruction` is `EXECUTE_SUBTASK` or `RETRY_SUBTASK` (critic-issued,
with `error_log` set and `attempt` bumped). Deserialisation is strict:
unknown verbs/statuses or missing required fields raise `SchemaError` — a
malformed message can never propagate silently.

### StateUpdate (result object)

```json
{
  "trace_id": "trace_8847aa01b2c3",
  "task_id": "task_0f1e2d3c4b5a",
  "agent_role": "backend_dev",
  "status": "SUCCESS",
  "result_pointers": ["<artifact-node-uuid>"],
  "summary": "Implemented error handling; tests pass locally.",
  "error_log": null,
  "metrics": { "wall_ms": 48211, "attempt": 1 },
  "attempt": 1,
  "task_payload": { "…the originating TaskPayload, round-tripped…" }
}
```

`status` ∈ `SUCCESS | FAILURE | RETRY | VALIDATED`. Results travel as matrix
*pointers*; the orchestrator resolves artifact content from the matrix.
`task_payload` rides along so the critic can re-dispatch autonomously.

## 4. Phase A sequence (implemented)

`mythos --swarm "Write a fibonacci script to /tmp/fib.py"`:

```
CLI → SwarmRuntime.start()            threads: orchestrator, backend_dev, critic
 1  Orchestrator seeds matrix         system_instruction + goal nodes (verbatim, trusted)
 2  Orchestrator → q.tasks.backend_dev        TaskPayload{EXECUTE_SUBTASK, pointers}
 3  Worker: navigate → fuse → MythosAgent.run  writes the file via its Tools API
 4  Worker upserts artifact node → q.critic.review   StateUpdate{SUCCESS, pointers}
 5  Critic validates (command / LLM verdict)
 5a   fail → q.tasks.backend_dev      TaskPayload{RETRY_SUBTASK, error_log verbatim, attempt+1}
      (loop 3–5 until pass or attempt == max_attempts, default 3)
 5b   pass → q.orchestrator.results   StateUpdate{VALIDATED}
 6  Orchestrator marks task done, dispatches next / returns the conclusion
```

Offline, no keys, no Docker — the identical flow over in-memory drivers:

```bash
python main.py --swarm --provider stub --bus inmemory --matrix inmemory "demo"
```

(Note: with the stub LLM the critic cannot obtain a real verdict, so the run
demonstrates the fail-safe path — three autonomous retries, then a reported
failure. That is the designed behaviour for unverifiable results.)

Phase A runs all agents as threads in one process, but **every boundary is a
real bus message** — splitting agents into separate processes or containers
is a deployment change, not a code change.

## 5. Roadmap

### Phase A — deterministic automation (this PR)
Rigid named workflows; sequential dispatch; structural critic loop; real
RabbitMQ + Qdrant with in-memory drivers behind the same interfaces; full
unit + integration test coverage.

### Phase B — dynamic orchestration (designed, not implemented)
* LLM-driven task decomposition in the orchestrator (replacing
  `workflows.py` lookups with generated `Workflow` objects) and runtime
  selection of agents/tools.
* Concurrent dispatch of independent plan branches (`Plan.depends_on`
  already models the DAG; `_wait_for` becomes a correlation map).
* Controlled "hallucination" for brainstorming inside agents, with hard
  guardrails at the boundary: strict schemas (already enforced), trust-score
  contradiction detection in the matrix, zero tolerance for fabricated data
  in execution paths.
* HTTP webhook adapter for `callback_queue` → true `callback_webhook`.
* Real token accounting from `LLMResponse.raw` usage (replacing the
  iteration-cap approximation), `access_level` enforcement.

### Phase C — always-on autonomy (designed, not implemented)
* Agents as separate processes/containers (compose services), always-on
  consumers; hard timeouts via process supervision.
* Self-initiated goals: monitors detect failures/opportunities and enqueue
  TaskPayloads without a human prompt; the human approves/rejects at the
  macro level.
* Matrix-driven learning: artifacts and failure reports accumulate into
  retrievable experience.

## 6. Deliberate deviations from the vision (Phase A)

| Vision | Phase A implementation | Why |
|---|---|---|
| `callback_webhook` (HTTP) | `callback_queue` (AMQP reply-to) | No HTTP server in Phase A; identical semantics. Webhook adapter is Phase B. |
| `max_compute_tokens` enforced as tokens | Iteration cap ≈ `budget // llm_max_tokens` | The existing Monitor counts iterations; true accounting needs provider usage extraction (Phase B). |
| Hard `timeout_ms` | Cooperative deadline (checked around the run) | A blocking LLM call cannot be killed mid-flight from a sibling thread; hard kills need process-per-agent (Phase C). |
| Kafka/RabbitMQ | RabbitMQ | Per-message acks and named queues map 1:1 to roles; Kafka's replay/throughput strengths are Phase C concerns. |
| Separate graph DB | Edges in Qdrant payloads | The vision's node schema embeds edges in the node; 1-hop traversal needs no Cypher. Revisit Neo4j in Phase B if queries grow. |
| Trust "overrides conflicting info" | Trust-ordered fusion (system first, verbatim) | Semantic contradiction *detection* is an open Phase B problem. |
