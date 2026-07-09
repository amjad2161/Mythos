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

## 5. Phase B — dynamic orchestration (IMPLEMENTED)

Phase B upgrades the deterministic Phase A swarm with adaptive routing,
governance, and specialised domain agents.  Patterns were adopted from the
owner's wider ecosystem (agency-agents runtime, Anthropic quickstarts).

### 5.1 Dynamic decomposition (`decomposer.py`)
Two-stage routing replaces the rigid workflow when ``--dynamic`` is set:

1. **Pre-filter** – deterministic keyword routing (`prefilter_roles`)
   narrows the candidate roles; `backend_dev` is always a candidate.
2. **LLM routing** – a cheap model (default `claude-haiku-4-5`) receives the
   goal + candidate roles with their tool lists and must return ONLY strict
   JSON `{"steps": [{role, objective, validation_command?,
   success_criteria?}], "rationale"}`.  Parsing is `SchemaError`-strict; one
   re-prompt carries the exact parse error verbatim; a second failure falls
   back to the configured deterministic workflow.

The output is an ordinary `Workflow` with `literal=True` steps (LLM text is
never `.format()`ed), so dynamic decomposition drives the *identical*
TaskPayload/queue pipeline — no new dispatch machinery.

### 5.2 Personas (`personas.py`, `personas/*.md`)
Each role carries a professional identity compiled from a Markdown persona
(strict frontmatter: name/role/mission/rules/success_metrics — the
agency-agents schema).  Personas append to the worker's system prompt via
`MythosConfig.system_suffix`; `MYTHOS_PERSONA_DIR` overlays custom personas
over the packaged five (backend_dev, critic, researcher, navigator, voice).

### 5.3 Real resource governance
* **Token accounting** — every provider response carries normalised
  `LLMResponse.usage`; the `Monitor` accumulates it and enforces
  `max_compute_tokens` as a REAL cumulative budget (plus a wall-clock
  deadline from `timeout_ms`), stopping mid-run rather than post-hoc.
* **Prompt caching** — the system prompt is sent as a `cache_control` block
  (Anthropic), cutting repeat-prefix cost across loop iterations.
* **Retries** — `RetryingLLM` wraps provider calls with exponential backoff
  on transient errors (rate limits, overload, connection blips).
* **CostGovernor** (`governor.py`) — sliding-hourly + per-run token budgets;
  when tripped, workers refuse new tasks with a structured FAILURE.

### 5.4 Task ledger (`ledger.py`)
Externalized durable progress (the autonomous-coding feature-list pattern):
one stable `MemoryNode` per goal records each step's role, objective,
task_id, status (pending → dispatched → validated/failed), attempts, and
summary.  Single-writer (orchestrator) by design — the matrix has no CAS.

### 5.5 New tools and roles
* `web_fetch` (`tools_web.py`) — SSRF-hardened: http/https only, every hop's
  DNS answers must be public, metadata endpoints blocked, manual redirect
  validation (≤5), 100 kB body cap.  Known stdlib limitation: DNS-rebinding
  TOCTOU (validation resolves separately from the connection).
* `think` — a no-side-effect reasoning scratchpad (quickstarts ThinkTool).
* **researcher** — web_fetch + files, deliberately no shell.
* **navigator** (`tools_geo.py`) — openrouteservice REST via stdlib urllib:
  `ors_geocode`, `ors_directions`, `ors_isochrones`, `ors_matrix`
  (`ORS_API_KEY`, or `MYTHOS_ORS_URL` for self-hosted).
* **voice** (`tools_tts.py`) — `speak` posts to any OpenAI-compatible
  `/v1/audio/speech` sidecar (reference: supertonic — MIT code, OpenRAIL-M
  weights); `docker compose --profile voice up`.
* `route_plan` builtin workflow: navigator → voice.

## 6. Roadmap (remaining)

### Phase B follow-ups
* Concurrent dispatch of independent plan branches (`Plan.depends_on`
  already models the DAG; `_wait_for` already buffers out-of-order results).
* Trust-score contradiction *detection* in the matrix (ordering is done).
* HTTP webhook adapter for `callback_queue` → true `callback_webhook`.
* `access_level` enforcement; matrix-similarity pre-filter for the router.

### Phase C — always-on autonomy (designed, not implemented)
* Agents as separate processes/containers (compose services), always-on
  consumers; hard kill-timeouts via process supervision.
* Self-initiated goals: monitors detect failures/opportunities and enqueue
  TaskPayloads without a human prompt; the human approves/rejects at the
  macro level.
* Matrix-driven learning: artifacts and failure reports accumulate into
  retrievable experience; work orders correlated via matrix nodes instead of
  riding on StateUpdates.

## 7. Deliberate deviations from the vision

| Vision | Implementation | Why |
|---|---|---|
| `callback_webhook` (HTTP) | `callback_queue` (AMQP reply-to) | No HTTP server yet; identical semantics. Webhook adapter is a follow-up. |
| `max_compute_tokens` enforced as tokens | **Done (Phase B)**: real cumulative accounting via `LLMResponse.usage` + Monitor | — |
| Hard `timeout_ms` | Cooperative mid-run deadline (Monitor checks between iterations) | A blocking LLM call cannot be killed mid-flight from a sibling thread; hard kills need process-per-agent (Phase C). |
| Kafka/RabbitMQ | RabbitMQ | Per-message acks and named queues map 1:1 to roles; Kafka's replay/throughput strengths are Phase C concerns. |
| Separate graph DB | Edges in Qdrant payloads | The vision's node schema embeds edges in the node; 1-hop traversal needs no Cypher. Revisit Neo4j if graph queries grow. |
| Trust "overrides conflicting info" | Trust-ordered fusion (system first, verbatim) + trace-scoped navigation | Semantic contradiction *detection* remains open. |
