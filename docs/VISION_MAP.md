# Mythos — Vision-to-Implementation Map

This document maps the owner's full autonomous-agent vision — layer by layer,
each on its own and all together — onto the shipped Mythos code. It is the
"ביחד ולחוד" (together and apart) reference: for every layer, the *need*, the
*implementation*, the *M2M protocol / permissions / performance* facts, and
what remains for later phases.

The vision's north star: move from a reactive prompt tool to a **living,
proactive digital workforce** — a network of distributed cognitions under
central management, delivering system-level engineering autonomously, as a
stepping stone toward user-focused AGI. Mythos is the reasoning core (NEURO)
of that federation.

---

## Layer 0 — Hardware & compute (vision) → deployment posture (Mythos)

**Need.** Local multi-agent execution without cloud bottlenecks; HEDT-class
CPU, large RAM for context windows, fast NVMe for vector DB, cloud fallback
for heavy reasoning.

**How Mythos meets it.** Mythos is deployment-agnostic and runs on any PC:
- **Local-first, cloud-optional.** The swarm process is lightweight Python;
  the LLM is pluggable (`llm.py`: `anthropic` | `openai` | `stub`). "Cloud
  fallback for heavy reasoning" is simply the default `anthropic` provider;
  local models drop in behind the same `BaseLLM` interface.
- **RAM for context** = the sliding-window short-term memory (`memory.py`)
  plus the Data Matrix for long-term recall — the vision's "context windows
  held in active memory."
- **NVMe + vector DB** = Qdrant with its persistent `qdrant_data` volume
  (`docker-compose.yml`).
- **Measured overhead** (`docs/PERFORMANCE.md`): the framework adds ~5 ms
  per subtask; the LLM dominates wall-clock by 3–5 orders of magnitude, so a
  single workstation is never the bottleneck for a personal swarm.

**Deferred:** true parallel *local* LLM inference across cores (HEDT
Threadripper scenario) is a hardware/provider choice, not a Mythos code item;
the `BaseLLM` seam already supports it.

---

## Layer 1 — Centralized Orchestrator ("Agent Boss")

**Need.** A super-router that ingests abstract *intents*, never executes work
itself, decomposes a concept into a matrix of sub-tasks, and routes each to a
dedicated agent with objectives, constraints, and success parameters.

**Implementation** (`orchestration/orchestrator.py`, `workflows.py`,
`decomposer.py`):
- **Intent intake + decomposition.** Rigid named workflows (Phase A) *or*
  LLM-driven dynamic decomposition (Phase B, `--dynamic`): a cheap routing
  model emits strict-JSON steps, each assigned to a role. The orchestrator
  maps the workflow onto the existing `Plan`/`Task` DAG.
- **Never executes.** The orchestrator only seeds memory, dispatches
  `TaskPayload`s, and collects critic-validated results. Work happens in
  workers exclusively.
- **Objectives/constraints/success params** = the `TaskParameters` +
  `Constraints` blocks on every payload (objective, success_criteria,
  validation_command; token/time/tool limits).
- **Concurrency** (Phase B): every ready DAG branch dispatches immediately
  (`_wait_for_any`); `depends_on` expresses parallel branches.
- **Multi-modal intake** (vision): text today; audio via the ASR tool
  (`tools_asr.py`) transcribes at the human boundary into a text intent.

**Performance/permissions.** Dispatch is a bus publish (~µs); the wait window
is an absolute monotonic deadline derived from the retry budget. The
orchestrator is the single writer of the task ledger.

**Deferred (Phase C):** self-initiated goals — the orchestrator enqueuing its
own TaskPayloads from detected failures/opportunities without a human prompt.

---

## Layer 2 — Data Matrix & Autonomous Navigation

**Need.** Long-term memory and absolute ground truth: a vector store holding
all instructions, code, history, and context **verbatim** (no information
loss), with autonomous traversal — an agent lacking a datum queries, extracts,
and continues without human help.

**Implementation** (`orchestration/matrix.py`, `schemas.py`):
- **Hybrid vector + knowledge graph** in one Qdrant collection: each
  `MemoryNode` carries an embedding, verbatim `content`, `metadata`
  (timestamp, source, `trust_score`, `verbatim_required`), and typed graph
  `edges` — exactly the vision's node schema.
- **Verbatim as ground truth.** `verbatim_required` content is reproduced
  exactly inside delimiters during fusion; system instructions carry
  `trust_score = 1.0` and always outrank conflicting lower-trust content.
- **Autonomous navigation** (`navigate`): embed the need → KNN search (+
  explicit pointers) → 1-hop graph traversal along edges → deduplicate →
  trust-ranked fusion into the exact context window — the vision's
  semantic-query → graph-expansion → data-fusion loop.
- **RAG + long-term updates.** Every worker artifact is upserted as a node
  (trace-tagged, edge-linked to its inputs), so each interaction enriches the
  matrix — "the system gets smarter interaction to interaction."
- **Resource dependencies** (adopted from HuggingGPT): a step's completed
  predecessors' artifact pointers are passed explicitly as the dependent's
  `context_pointers`, so data flows along dependency edges, not just ordering.

**Performance.** KNN on the in-memory driver is brute-force (~80 ms at 2k
nodes); the production Qdrant driver uses an ANN index for single-digit-ms
search at scale. Trace-scoping prevents stale goals from leaking across runs.

**Deferred (Phase B/C):** semantic *contradiction detection* between nodes
(ordering is done); matrix-similarity role pre-filter.

---

## Layer 3 — Agentic Swarm (Execution Workers) + Critic

**Need.** A digital-coworker team specialized by discipline; each agent has a
defined Tools API; **inter-agent comms are programmatic only** (JSON via
webhooks/API, never prose); mutual-critique loops where a critic verifies the
executor's output, with autonomous iterative debugging until validated.

**Implementation** (`orchestration/worker.py`, `critic.py`, `roles.py`,
`personas/`):
- **Specialized digital coworkers** with virtual identities: five roles, each
  a persona (Markdown identity/mission/rules) + an enforced tool subset —
  `backend_dev` (code/shell), `researcher` (web, no shell), `navigator`
  (geo), `voice` (TTS+ASR), `critic` (read/execute-only).
- **Per-role Tools API** (`roles.py`): the registry is filtered per role;
  unknown tools fail at startup. `access_level` (restricted/standard/elevated)
  further gates mutation.
- **Programmatic M2M only.** Agents exchange strict `TaskPayload`/`StateUpdate`
  JSON envelopes over RabbitMQ queues — never natural language. Malformed
  messages raise `SchemaError` (never propagate silently). This is the
  vision's "communication in code, not English/Hebrew."
- **Mutual-critique loop.** Every worker result is structurally intercepted by
  the critic (topology enforces it); on failure the exact error output is
  injected verbatim and the task re-dispatched autonomously (up to
  `max_attempts`) — the vision's "iterative debugging until full validation,
  only then the result rises to the Agent Boss." No human, no orchestrator in
  that loop.
- **24/7 availability.** Workers are long-lived queue consumers; the swarm is
  always ready for the next goal.

**Permissions.** Least privilege: critic can never write; researcher can never
shell; `forbidden_modules` and `access_level: restricted` strip
write/execute tools per task; guardrails (`guardrails.py`) block
system-destructive file/shell operations ("zero tolerance for deleting
critical system files").

**Deferred (Phase C):** agents as separate OS processes/containers with hard
kill-timeouts (currently threads with cooperative deadlines).

---

## Layer 4 — M2M Communication Protocols

**Need.** No natural language between machines; JSON objects, webhooks, access
tokens; standardized task payloads with trace ids, roles, context pointers,
constraints, callbacks; a critic feedback loop with verbatim error logs.

**Implementation** (`orchestration/schemas.py`, `bus.py`):
- **TaskPayload** = the exact vision envelope: `system_instruction`,
  `trace_id`, `orchestrator_node`, `target_agent{role, access_level}`,
  `task_parameters{objective, context_pointers, ...}`,
  `constraints{max_compute_tokens, forbidden_modules, timeout_ms}`,
  `callback_queue`, `attempt`, `error_log`.
- **StateUpdate** = the structured result object (not free text) with status,
  result *pointers* (not bulk content), verbatim `error_log`, metrics.
- **Context pointers, not re-sent text** — control messages carry matrix node
  ids, honoring the vision's bandwidth optimization.
- **Transport.** RabbitMQ durable queues + manual acks (production) with an
  in-memory driver of the identical contract (offline/tests). Long handlers
  run off-thread so heartbeats never starve.
- **Real-time push (adopted):** an `EventHub` fans lifecycle events out to
  subscribers; the control panel streams them to the browser over **SSE**
  (`/api/events`) — immediate bidirectional-feeling updates, replacing
  polling. Tokens/webhooks and outbound `callback_webhook` are the documented
  next integration step.

**Performance.** In-process bus ~120–226k msgs/sec; RabbitMQ handles the
swarm's handful-per-subtask volume trivially. SSE pushes events with
sub-tick latency instead of the 1.5–2.5 s poll interval.

---

## Layer 5 — Front-End & Interactive UX

**Need.** A control bridge between the digital entities and the human
operator; a visual representation of complex logic; real-time status, agent
relationship networks, and system alerts — soft, approachable aesthetics to
reduce cognitive load.

**Implementation** (`orchestration/server.py`, `mythos --serve`):
- **Local control panel** on 127.0.0.1:8642 (dependency-free stdlib server):
  submit goals, watch a serial run queue, and see **live per-step Task Ledger
  progress** with a real-time SSE event stream and event log.
- **Real-time status** = the SSE feed (`goal.started` → `task.dispatched` →
  `task.validated`/`failed` → `goal.completed`) plus `/api/status`
  (backends, roles, hourly token spend).
- **Voice at the boundary** = `speak` (TTS out) and `transcribe` (ASR in),
  so the human can talk to and be answered by the swarm.

**Deferred (design intent):** a 3D/Pixar-style spatial HUD (Spline/OpenDesign)
visualizing the agent network. The SSE event stream is the data backbone such
a front-end would consume; building the 3D surface is a separate UI project
that plugs into `/api/events` and `/api/runs` unchanged.

---

## Layer 6 — Guardrails, Feedback Loops & Continuous Learning

**Need.** Strict boundaries against hallucinations and dangerous actions (no
deleting critical files, no wrong locations); every successful task
vectorized into the matrix as new "memory"; verbatim retention as the basis
for future decisions.

**Implementation:**
- **Guardrails** (`guardrails.py`): a deny-list of protected system paths and
  destructive shell patterns, enforced in the file/shell tools; per-role tool
  restriction; `access_level: restricted`; cost circuit breaker
  (`governor.py`) as financial blast-radius control.
- **Feedback loops** = the critic retry loop (Layer 3) + the Monitor's
  iteration/failure/loop/token/deadline caps — bounded autonomy, no run is
  ever unbounded.
- **Continuous learning** = artifacts and failure reports upserted verbatim
  into the matrix, trust-scored and trace-tagged, retrievable by future runs.

---

## The three-phase roadmap (vision) → status (Mythos)

| Phase | Vision | Status |
|---|---|---|
| **A — deterministic automation** | Initial agents wired to tools; rigid workflows (A→B); stable, reliable substrate | **Shipped** |
| **B — dynamic thinking & adaptive multi-agent** | Router chooses agents/tools/priorities at runtime; controlled creativity with hard guardrails; concurrency | **Shipped** (`--dynamic`, concurrent DAG, governor, guardrails) |
| **C — perceptual simulation toward local AGI** | Always-on background autonomy; agents detect failures/opportunities, self-initiate, self-implement; human is macro-approver | **Designed, deferred** — process-per-agent, self-initiated goals, approval queue, matrix-driven learning |

---

## Risk vectors (vision) → controls (Mythos)

The vision explicitly flags deception, rebellion, and existential risk as
autonomy rises. Mythos's concrete, shipped controls that bound these at the
personal-PC scale (full analysis in `docs/SECURITY.md`):

- **Bounded autonomy** — every loop terminates (iteration/failure/token/
  deadline caps, critic retry cap); no unbounded self-modification path.
- **Structural verification** — nothing an agent produces reaches the user
  without passing the critic; unverifiable results fail closed.
- **Least privilege + guardrails** — role tool-subsets, `access_level`,
  `forbidden_modules`, protected-path/destructive-command blocks.
- **Financial containment** — the cost governor caps spend as a hard ceiling.
- **Auditability** — strict M2M schemas, `trace_id` correlation, and the
  durable ledger make every action reconstructable.
- **Human at the macro level** — the operator is the Agent Boss who submits
  and (in Phase C) approves; the panel is the approval surface.

These are engineering controls appropriate to a single-user local system;
they do not solve frontier alignment, and the security doc names the residual
risks accepted for this scale.
