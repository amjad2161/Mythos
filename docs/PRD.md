# Mythos — Product Requirements Document (PRD)

| | |
|---|---|
| **Product** | Mythos — autonomous multi-agent system ("NEURO" reasoning core of the SINGULARITY/JARVIS federation) |
| **Document version** | 1.0 |
| **Product version covered** | v0.2.0 (Phases A + B + Local Install Layer) |
| **Status** | Approved for docs/PRD.md |
| **Owner** | Product/Requirements Analyst, Mythos engineering team |
| **Date** | 2026-07-09 |

---

## 1. Problem Statement & Need

A single power-user running serious automation on a personal PC today faces a structural gap: LLM chat assistants can *reason* but cannot *deliver* — they hold no durable memory, execute no multi-step work unattended, verify nothing, and offer no cost control. Conversely, existing agent frameworks are heavyweight, cloud-first, and opaque: they demand always-on infrastructure, hide their message flows, and pass unvalidated free text between components, making failures silent and unreproducible.

The owner's need (documented in his Hebrew architecture papers and SINGULARITY profile) is a **digital workforce for one person's PC**: he states an intent once, and a network of specialist agents plans, executes, self-verifies, self-retries, and reports back — with him acting as top-level approver ("Agent Boss"), not as an operator babysitting each step.

Concretely, the pain removed:

- **Operator toil.** Multi-step goals (write code, research, plan a route, produce audio) currently require the human to sequence, paste context, and re-prompt at every step. Mythos decomposes and routes autonomously (`orchestrator.py`, `decomposer.py`).
- **Unverified output.** Chat output is accepted on faith. Mythos interposes a structural critic that validates every result mechanically (`validation_command`, exit 0) before it ever reaches the orchestrator, and autonomously feeds exact failure output back to the worker for retry (`critic.py`).
- **Context amnesia.** Every session starts cold. Mythos keeps one ground truth in a hybrid vector + knowledge-graph Data Matrix with trust-scored, verbatim nodes (`matrix.py`).
- **Uncontrolled spend.** API costs are invisible until the invoice. Mythos meters real token usage per response and enforces hourly and per-run budgets with a circuit breaker (`governor.py`, `monitor.py`).
- **Fragile free-text glue.** Agent-to-agent prose is a silent failure mode. Mythos permits **no free text between machines** — only strict JSON envelopes whose deserialisation raises `SchemaError` on any malformation (`schemas.py`).
- **Cloud lock-in.** The entire system installs, runs, and demos on one PC — including a fully offline mode with zero API keys and zero Docker (`--provider stub --bus inmemory --matrix inmemory`).

## 2. Target User & Personas

### P1 — The Agent Boss (primary; the owner)

A technical power-user on a personal Windows/Linux PC. Fluent in Python and Docker, allergic to babysitting tools. He wants to submit abstract goals ("write and test this script", "plan the Haifa–Eilat route and narrate it") and approve or reject at the macro level. He values: one-command install, a local browser panel he can glance at, hard cost ceilings, offline demonstrability, and a system whose message flows he can read and audit. He is also the system's developer, so `--doctor` diagnostics and deterministic offline tests matter to him directly.

### P2 — The Federation Integrator (primary, same person, different hat)

Mythos is the NEURO reasoning core of a larger SINGULARITY/JARVIS federation. In this role the owner consumes Mythos programmatically: `SwarmRuntime` as an embeddable Python API, RabbitMQ queues as integration points, the Data Matrix as shared memory for sibling systems, and personas (`MYTHOS_PERSONA_DIR`) as the extension mechanism for new specialist identities.

### P3 — Small technical teams (future, v0.4+)

2–10 person teams sharing one swarm: multiple submitters, per-user budgets, role-scaled workers (N workers per role), and access-level enforcement on TaskPayloads. Explicitly **out of scope** for v0.2.0/v0.3; noted here so current design decisions (durable queues, `access_level` field already in the protocol, process-per-agent Phase C plan) keep this door open.

## 3. Product Goals & Non-Goals

### Goals

- **G1 — Autonomous delivery.** A single abstract goal is decomposed, routed to specialist agents, executed, validated, and retried without human intervention between submission and terminal result.
- **G2 — Structural quality.** Every worker result passes through the critic quality gate; no unvalidated artifact reaches the orchestrator or user.
- **G3 — One ground truth.** All durable knowledge (system instructions, goals, artifacts, ledgers) lives verbatim and trust-scored in the Data Matrix; agents navigate it autonomously.
- **G4 — Strict machine protocol.** All inter-agent communication is schema-validated JSON over an async bus; malformed messages fail loudly, never silently.
- **G5 — Governed cost.** Token spend is metered from real provider usage data and capped by per-run and sliding-hourly budgets; the system stops mid-run, not post-invoice.
- **G6 — PC-native operation.** One-command launch, localhost-only web panel, env-file config, environment doctor, and a complete offline mode requiring neither API keys nor Docker.
- **G7 — Reuse over reinvention.** The multi-agent layer wraps the proven single-agent core (`MythosAgent`, `Planner`, `Monitor`, `Executor`) rather than duplicating it.

### Non-Goals (explicit)

- **NG1 — Not a SaaS.** No multi-tenant hosting, no public endpoints, no accounts. The panel binds to 127.0.0.1 by design.
- **NG2 — Not always-on (yet).** Self-initiated goals, process-per-agent supervision, and hard kill-timeouts are Phase C (designed, deferred).
- **NG3 — Not a general chat assistant.** Mythos executes goals; it is not optimised for open-ended conversation.
- **NG4 — Not a sandbox.** `run_shell` executes arbitrary commands by design; containment is the operator's responsibility (documented in README safety notes).
- **NG5 — No semantic contradiction detection.** Trust handling in v0.2.0 is ordering-based fusion only; contradiction *detection* is roadmap.
- **NG6 — No GUI beyond the panel.** No desktop app, no mobile app, no voice *input* (voice is output-only via the TTS sidecar).
- **NG7 — No fine-tuning or model training.** Mythos orchestrates hosted/stub LLMs; it does not train them.

## 4. Core User Journeys

### J1 — One-command install and first launch

- **Preconditions:** Fresh PC with Python 3.9+; Docker optional; repository cloned.
- **Steps:** (1) Run `./scripts/launch.sh` (or `.\scripts\launch.ps1`; `--offline` / `-Offline` for no-Docker). (2) Launcher creates a venv, installs Mythos, writes `~/.mythos/env` template, starts RabbitMQ + Qdrant if Docker is present, runs the doctor. (3) User pastes `ANTHROPIC_API_KEY` into `~/.mythos/env`. (4) Browser opens the control panel at http://127.0.0.1:8642.
- **Success criteria:** Panel reachable on localhost; `python main.py --doctor` exits 0 (or reports only optional-service warnings); total time under 10 minutes on a typical connection.

### J2 — Submit a coding goal from the browser and watch the ledger

- **Preconditions:** Panel running (`python main.py --serve`); valid API key; bus/matrix backends up (or in-memory).
- **Steps:** (1) User enters "Write a Python script that prints the Fibonacci sequence to /tmp/fib.py" in the panel. (2) The run enters the serial queue on the shared `SwarmRuntime`. (3) Orchestrator seeds the matrix, dispatches a `TaskPayload` to `q.tasks.backend_dev`. (4) Worker navigates the matrix, writes the file, upserts an artifact node. (5) Critic runs the `validation_command`; on failure, verbatim error output loops back as `RETRY_SUBTASK` — visible as an attempt bump. (6) User watches each ledger step move pending → dispatched → validated live in the panel.
- **Success criteria:** `/tmp/fib.py` exists and runs; ledger shows all steps `validated`; final conclusion rendered in the panel; no manual intervention after submission.

### J3 — Offline demo without keys (fail-safe path)

- **Preconditions:** None — no API key, no Docker, no network.
- **Steps:** (1) Run `python main.py --swarm --provider stub --bus inmemory --matrix inmemory "demo"`. (2) The identical orchestrator → worker → critic pipeline runs over in-memory drivers. (3) The stub LLM cannot produce a real verdict, so the critic exercises the designed fail-safe: three autonomous retries, then a reported failure.
- **Success criteria:** Run terminates cleanly with a structured failure report (never a hang or crash); retry count equals `max_attempts` (3); demonstrates every protocol boundary offline in under 60 seconds.

### J4 — Dynamic route + voice briefing

- **Preconditions:** API key set; `ORS_API_KEY` (or `MYTHOS_ORS_URL`) and `MYTHOS_TTS_URL` (e.g. `docker compose --profile voice up`) configured; `--doctor` shows nav/voice green.
- **Steps:** (1) Run `python main.py --swarm --dynamic "Plan a driving route from Haifa to Eilat and narrate it as audio"`. (2) The routing model (default `claude-haiku-4-5`) decomposes the goal into role-assigned steps as strict JSON — navigator then voice. (3) Navigator calls `ors_geocode`/`ors_directions`; voice posts the narration to the TTS sidecar via `speak`. (4) Critic validates each step; orchestrator returns the conclusion.
- **Success criteria:** Decomposer emits schema-valid steps on attempt 1 or 2 (else deterministic fallback to `route_plan`); audio artifact produced; both artifacts pointer-linked in the Data Matrix.

### J5 — Interactive swarm session with shared memory

- **Preconditions:** API key set; services up (or in-memory flags).
- **Steps:** (1) Run `python main.py --swarm` with no goal. (2) At `Swarm goal >`, submit goal 1; the runtime and its Data Matrix persist. (3) Submit goal 2 that builds on goal 1's artifacts (matrix navigation surfaces them via semantic search + 1-hop graph traversal). (4) Exit with `exit`/Ctrl-C; runtime shuts down cleanly.
- **Success criteria:** Goal 2's fused context contains goal 1 artifacts without the user pasting anything; clean shutdown releases bus consumers and threads.

### J6 — Diagnose a broken environment

- **Preconditions:** Something is wrong — missing key, stopped Docker service, absent optional package.
- **Steps:** (1) Run `python main.py --doctor`. (2) Doctor reports API key presence, package availability, RabbitMQ/Qdrant reachability, and voice/nav configuration, each with pass/warn/fail. (3) User fixes the flagged item (e.g. `python main.py --init` to regenerate the env template, `docker compose up -d`). (4) Re-run doctor to confirm.
- **Success criteria:** Every failure carries an actionable message; exit code distinguishes healthy from broken; a first-time user reaches a green (or green-with-optional-warnings) report without reading source code.

### J7 — Cost circuit breaker under budget pressure

- **Preconditions:** `MYTHOS_HOURLY_TOKEN_BUDGET` / `MYTHOS_RUN_TOKEN_BUDGET` set low deliberately; API key valid.
- **Steps:** (1) Submit a token-hungry goal via `--swarm`. (2) The Monitor accumulates real `LLMResponse.usage` per call and stops the run when the cumulative budget or `timeout_ms` deadline trips. (3) `CostGovernor`'s sliding-hourly window trips; subsequent workers refuse new tasks with a structured FAILURE. (4) Budget window elapses; work is accepted again.
- **Success criteria:** Run stops **mid-flight**, not after completion; refusal is a schema-valid FAILURE StateUpdate (never an exception or silent drop); spend never exceeds budget by more than one LLM call's `max_tokens`.

## 5. Functional Requirements

Each requirement maps to its implementing module (all paths relative to `mythos/`).

| ID | Requirement | Module |
|---|---|---|
| FR-1 | The system SHALL run a single autonomous agent loop (plan → act → observe → reflect) for a free-text goal, terminating via the `finish` tool, iteration cap, or failure cap. | `agent.py`, `executor.py`, `planner.py`, `monitor.py` |
| FR-2 | The orchestrator SHALL decompose a goal into role-addressed subtasks and dispatch them as `TaskPayload` messages, never executing work itself; every ready DAG branch SHALL be dispatched concurrently. | `orchestration/orchestrator.py`, `orchestration/workflows.py` |
| FR-3 | All inter-agent messages SHALL be strict JSON envelopes (`TaskPayload`, `StateUpdate`); deserialisation SHALL raise `SchemaError` on unknown verbs/statuses or missing required fields. | `orchestration/schemas.py` |
| FR-4 | The message bus SHALL provide durable named queues (`q.tasks.<role>`, `q.critic.review`, `q.orchestrator.results`) with manual acks over RabbitMQ, and an in-memory implementation of the identical contract. | `orchestration/bus.py` |
| FR-5 | The Data Matrix SHALL store `MemoryNode`s combining embedding vector, verbatim content, trust score, and graph edges in one Qdrant collection, with an in-memory equivalent. | `orchestration/matrix.py` |
| FR-6 | `DataMatrix.navigate` SHALL fuse context via semantic KNN + explicit pointers + 1-hop edge traversal, deduplicated and trust-ordered, reproducing `verbatim_required` content exactly. | `orchestration/matrix.py` |
| FR-7 | Workers SHALL wrap the single-agent core with a per-role filtered Tools API, honouring payload `constraints` (`forbidden_modules`, `max_compute_tokens`, `timeout_ms`), and publish results as matrix pointers. | `orchestration/worker.py`, `orchestration/roles.py` |
| FR-8 | Every worker result SHALL be intercepted by the critic (read/execute-only tools): mechanical validation via `validation_command` first, LLM verdict fallback (`VERDICT: PASS/FAIL`), fail-safe on no verdict; failures SHALL re-dispatch to the worker with verbatim `error_log` up to `max_attempts` (default 3) without orchestrator involvement. | `orchestration/critic.py` |
| FR-9 | With `--dynamic`, a cheap routing LLM SHALL decompose the goal into strict-JSON role steps; one re-prompt SHALL carry the exact parse error; a second failure SHALL fall back to the named deterministic workflow. | `orchestration/decomposer.py` |
| FR-10 | Each role SHALL carry a Markdown persona (strict frontmatter) appended to the worker system prompt; `MYTHOS_PERSONA_DIR` SHALL overlay the packaged five. | `orchestration/personas.py`, `orchestration/personas/` |
| FR-11 | Token spend SHALL be accounted from real provider `usage` data; the Monitor SHALL enforce cumulative per-run budgets and deadlines mid-run; `CostGovernor` SHALL enforce sliding-hourly + per-run ceilings, refusing new tasks with structured FAILUREs when tripped. | `llm.py`, `monitor.py`, `orchestration/governor.py` |
| FR-12 | Each goal SHALL maintain a durable, single-writer task ledger node in the matrix recording per-step role, objective, status, attempts, and summary. | `orchestration/ledger.py` |
| FR-13 | The system SHALL support pluggable LLM providers (`anthropic` default, `openai`, deterministic `stub`), with Anthropic prompt caching and exponential-backoff retry on transient errors. | `llm.py` |
| FR-14 | Specialist tools SHALL include SSRF-hardened `web_fetch` (public-DNS-only, redirect-validated, 100 kB cap), openrouteservice geo tools, OpenAI-compatible TTS `speak`, and a no-side-effect `think` scratchpad. | `tools_web.py`, `tools_geo.py`, `tools_tts.py`, `tools.py` |
| FR-15 | `--serve` SHALL start a dependency-free localhost web control panel: goal submission, a serial run queue over one shared `SwarmRuntime`, and live per-run Task Ledger progress. | `orchestration/server.py` |
| FR-16 | `--init` SHALL write a config template to `~/.mythos/env`; `~/.mythos/env` and `./.env` SHALL load automatically with exported variables winning. | `envfile.py` |
| FR-17 | `--doctor` SHALL diagnose API key, packages, RabbitMQ/Qdrant reachability, and voice/nav configuration, with actionable messages and meaningful exit codes. | `doctor.py` |
| FR-18 | One-command launchers SHALL provision venv, install, config, optional Docker services, doctor, and panel on Linux/macOS and Windows, with an offline mode. | `scripts/launch.sh`, `scripts/launch.ps1` |
| FR-19 | The CLI SHALL expose single-agent, `--swarm` (one-shot + interactive), `--dynamic`, `--workflow`, `--bus`/`--matrix` backend selection, `--serve`, `--init`, `--doctor`, and `--version`. | `main.py` |
| FR-20 | Users SHALL be able to register custom tools on the single agent via the public `Tool` API. | `tools.py`, `agent.py` |

## 6. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | **Performance — offline demo.** J3 (stub/in-memory swarm, including 3 retries) completes in < 60 s on commodity hardware. |
| NFR-2 | **Performance — panel.** Control panel first paint < 1 s on localhost; ledger progress visible without manual refresh; goal submission acknowledged < 500 ms (execution is queued, not blocking). |
| NFR-3 | **Performance — routing.** Dynamic decomposition adds at most 2 routing-model calls per goal (initial + one re-prompt) before deterministic fallback. |
| NFR-4 | **Reliability — no silent corruption.** 100% of malformed inter-agent messages raise `SchemaError`; zero tolerance for silently propagated bad envelopes (verified by `tests/orchestration/test_schemas.py`). |
| NFR-5 | **Reliability — bounded autonomy.** Every run terminates: iteration cap (default 50), consecutive-failure cap (default 5), repetitive-call loop detection, critic retry cap (3), cooperative deadline from `timeout_ms`. No configuration yields an unbounded run. |
| NFR-6 | **Reliability — delivery.** Bus messages are durable with manual acks; a crashing handler triggers redeliver-once-then-drop, never poison-message loops. |
| NFR-7 | **Cost.** Hourly and per-run token ceilings are hard limits enforced from real provider usage; overshoot bounded by one call's `max_tokens`. Prompt caching applied to the system prompt on the Anthropic backend. |
| NFR-8 | **Security — network.** Web panel binds to 127.0.0.1 by default; `web_fetch` enforces http/https only, public-DNS resolution per hop, metadata-endpoint blocking, ≤5 validated redirects, 100 kB body cap (known limitation: DNS-rebinding TOCTOU, documented). |
| NFR-9 | **Security — least privilege.** Critic tools are read/execute-only; per-role tool filtering plus payload `forbidden_modules` restrict workers; researcher role has no shell. `run_shell` risk is prominently documented. |
| NFR-10 | **Offline capability.** Full swarm pipeline (all queues, matrix, critic loop, ledger) runs with zero API keys, zero Docker, zero network via stub/in-memory drivers — bit-for-bit the same message contracts. |
| NFR-11 | **Observability.** Every run traceable via `trace_id`/`task_id` across all envelopes; per-goal ledger durable in the matrix; `StateUpdate.metrics` records wall time and attempts; verbose mode streams step progress; doctor reports environment health. |
| NFR-12 | **Portability.** Python 3.9+; Linux, macOS, Windows (PowerShell launcher); core install is dependency-light (anthropic SDK only), orchestration extras opt-in (`pip install -e ".[orchestration]"`). |
| NFR-13 | **Testability.** Unit + orchestration suites run offline in CI with no secrets; integration tests against live RabbitMQ/Qdrant are marked and opt-in (`-m integration`). |
| NFR-14 | **Data integrity.** `verbatim_required` matrix content is never paraphrased; system instructions (trust 1.0) always outrank lower-trust content in fused context. |

## 7. Success Metrics / KPIs

| Metric | Definition | Target (v0.2.0) |
|---|---|---|
| First-attempt validation rate | % of subtasks passing the critic on attempt 1 (live provider, code_delivery goals) | ≥ 70% |
| Goal completion rate | % of well-formed goals reaching VALIDATED terminal state within retry budget | ≥ 90% |
| Cost per goal | Mean total tokens per completed reference coding goal | ≤ 150k tokens, always ≤ `MYTHOS_RUN_TOKEN_BUDGET` |
| Time-to-first-artifact | Goal submission → first artifact node upserted (live, J2 reference goal) | ≤ 3 min |
| Install-to-first-goal | Fresh clone → first validated goal via launcher (J1 + J2) | ≤ 15 min |
| Autonomy ratio | Human interventions between submission and terminal result | 0 |
| Retry efficacy | % of critic-failed subtasks recovered by autonomous retry (attempts 2–3) | ≥ 50% |
| Router precision | % of `--dynamic` decompositions that are schema-valid without fallback | ≥ 90% |
| Budget compliance | Runs exceeding configured token ceiling by more than one call's `max_tokens` | 0 |
| Offline demo reliability | J3 clean structured termination rate | 100% |
| Cache savings | Input-token reduction from prompt caching on multi-iteration runs | ≥ 30% |
| Test health | Offline suite pass rate in CI | 100% |

## 8. Release Criteria & Roadmap

### 8.1 Release criteria — v0.2.0 (this PR)

Ship when all of the following hold:

- **RC-1** All FR-1 … FR-20 implemented and covered by the offline test suite; `python -m pytest tests/ -q` passes with no network and no secrets.
- **RC-2** Integration suite (`pytest tests/integration -m integration`) passes against `docker compose up -d` RabbitMQ + Qdrant.
- **RC-3** Journeys J1–J7 manually verified on Linux and Windows; J3 verified on a machine with no Docker and no keys.
- **RC-4** `--doctor` produces actionable output for each failure class it checks (missing key, missing package, unreachable service, unconfigured voice/nav).
- **RC-5** Budget enforcement demonstrated: a run stopped mid-flight by the Monitor and a task refused by a tripped `CostGovernor`, both as structured outcomes (J7).
- **RC-6** Documentation current: README, docs/ARCHITECTURE.md (Phase B marked implemented, deviations table accurate), this PRD at docs/PRD.md.
- **RC-7** `mythos.__version__` bumped to `0.2.0` (currently `0.1.0` in `mythos/__init__.py`); `--version` reports it.
- **RC-8** No known silent-failure defects: every documented failure mode terminates in a schema-valid FAILURE or a raised, user-visible error.

### 8.2 v0.3 wishlist (mapped to the documented roadmap)

**Phase B follow-ups (from docs/ARCHITECTURE.md §6):**

- **W-1** Trust-score contradiction *detection* in the Data Matrix (fusion ordering exists; flag semantic conflicts between nodes).
- **W-2** HTTP webhook adapter for `callback_queue` → true `callback_webhook`, closing the deviation from the original vision protocol.
- **W-3** `access_level` enforcement on `TaskPayload.target_agent` (field exists in the protocol; unenforced today).
- **W-4** Matrix-similarity pre-filter for the dynamic router (retrieval-informed role candidates alongside keyword pre-filtering).

**Phase C — always-on autonomy (designed, not implemented):**

- **W-5** Agents as separate processes/containers (compose services), always-on queue consumers; hard kill-timeouts via process supervision — replacing the cooperative-deadline limitation.
- **W-6** Self-initiated goals: monitors detect failures/opportunities and enqueue TaskPayloads without a human prompt; the Agent Boss approves/rejects at the macro level (panel approval queue).
- **W-7** Matrix-driven learning: artifacts and failure reports accumulate into retrievable experience; work orders correlated via matrix nodes instead of riding on StateUpdates.
- **W-8** N-workers-per-role horizontal scaling (parallelism today is across roles; per-role scale-out becomes a deployment knob under process-per-agent).

**Panel & operability (extending the Local Install Layer):**

- **W-9** Panel v2: run history across restarts, per-run token/cost display from ledger + governor data, cancel button for a queued/running goal.
- **W-10** Bus/matrix health surfaced in the panel (doctor-as-a-service endpoint).

Non-goals NG1–NG7 remain in force through v0.3; persona P3 (small teams) is earliest v0.4, gated on W-3 and W-5.

---

*End of document.*
