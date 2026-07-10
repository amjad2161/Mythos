# Mythos — Test Strategy & Coverage Audit

**Owner:** QA Lead · **Scope:** `mythos/` + `tests/` at repo root · **Status:** Living document, reviewed each release

---

## 1. Test Architecture As-Built

The suite is layered so that every behavioural contract is proven cheaply and deterministically first, and against real infrastructure second.

### 1.1 The four tiers

| Tier | Location | Doubles / backends | Selection |
|---|---|---|---|
| **Unit — single-agent core** | `tests/test_*.py` | `StubLLM` (scripted responses), fake SDK modules injected into `sys.modules` (`test_providers_fake.py`), mocked HTTP (`test_tools_web.py`, `test_tools_geo.py`, `test_tools_tts.py`) | default `pytest` |
| **Unit — orchestration** | `tests/orchestration/` | `InMemoryBus`, `InMemoryDataMatrix`, `HashEmbedder`, `StubLLM` via injected `llm_factory` | default `pytest` |
| **Bridge — qdrant-client `:memory:`** | `tests/orchestration/test_matrix_qdrant_local.py` | Real `QdrantDataMatrix` code over qdrant-client's in-process `:memory:` mode; `pytest.importorskip("qdrant_client")` | default `pytest` (skips if lib absent) |
| **Integration — live services** | `tests/integration/` | Live RabbitMQ + Qdrant (see `docker-compose.yml`) | `-m integration` only |

### 1.2 Gating

- `pytest.ini` registers the `integration` marker and sets `addopts = -m "not integration"`, so a plain local `pytest` never touches the network.
- `tests/integration/conftest.py` adds a second safety layer: session fixtures `broker_url` / `qdrant_url` probe TCP reachability (and Qdrant's `/readyz`) and **skip** when a service is absent, so even an explicit `-m integration` run degrades gracefully rather than erroring. The `unique_name` fixture suffixes queues/collections per test to prevent cross-run collisions.

### 1.3 CI (`.github/workflows/ci.yml`)

- **`test` job:** unit suite on a Python **3.9 / 3.10 / 3.11 / 3.12** matrix (`fail-fast: false`), `pip install -e ".[dev]"`, `pytest tests/ -q`.
- **`integration` job:** Python 3.12 with `rabbitmq:3.13-management` and `qdrant/qdrant:v1.12.4` service containers, an explicit `/readyz` wait loop, `MYTHOS_BROKER_URL` / `MYTHOS_QDRANT_URL` env, and `pytest tests/integration -m integration -q`.

### 1.4 Why this layering works

1. **Determinism.** `StubLLM` scripts exact `LLMResponse` sequences and `HashEmbedder` is a pure function of the text, so KNN ranking, retry loops, and verdict handling are fully reproducible — no flaky model calls, no embedding drift.
2. **No network in the unit tier.** All external surfaces (Anthropic/OpenAI SDKs, ORS geocoding, TTS sidecar, web fetch) are faked at the module or `urlopen` boundary; the unit tier runs identically on a laptop, on 3.9, and in CI.
3. **Contract symmetry.** `InMemoryBus` intentionally honours the same at-least-once / redeliver-once-then-drop contract as `RabbitMQBus`, and `test_matrix_qdrant_local.py` runs the *real* Qdrant driver code without infrastructure — the integration tier then only has to prove that the same contracts hold over real sockets (`test_rabbitmq_bus.py`, `test_qdrant_matrix.py`, `test_swarm_live.py` mirror their offline twins).
4. **Shared factories.** `tests/orchestration/conftest.py` centralises `make_agent_config` (stub provider), `make_orch_config` (in-memory backends, hash embedder) and `make_payload` (canonical `EXECUTE_SUBTASK`), so a schema/config change is edited once.

---

## 2. Coverage Map

| Module | Test file(s) | What is covered |
|---|---|---|
| `mythos/orchestration/schemas.py` | `tests/orchestration/test_schemas.py` | `TaskPayload`/`StateUpdate`/`MemoryNode` JSON round-trips; strict rejection (unknown instruction/status, missing `trace_id`, non-JSON, non-object); defaults; verbatim `error_log` preservation |
| `mythos/orchestration/bus.py` (`InMemoryBus`) | `tests/orchestration/test_bus_inmemory.py` | publish/consume ordering, **crash → redeliver once → drop**, queue isolation, stop-event termination, `task_queue()` naming |
| `mythos/orchestration/bus.py` (`RabbitMQBus`) | `tests/integration/test_rabbitmq_bus.py` | Same contract against a live broker + cross-thread publishing (integration only — see gap G1) |
| `mythos/orchestration/matrix.py` | `tests/orchestration/test_matrix_inmemory.py`, `test_matrix_qdrant_local.py`, `tests/integration/test_qdrant_matrix.py` | HashEmbedder determinism/normalisation; KNN; graph traversal + hop limit; trust ranking (system > user > agent); **trace scoping** (other-trace exclusion, seed-pointer bypass); `fuse_context` verbatim delimiters; Qdrant payload round-trip |
| `mythos/orchestration/roles.py`, `worker.py` | `tests/orchestration/test_worker.py`, `test_worker_budget.py` | Role tool allow-lists, forbidden-module stripping, `finish` unremovable, access levels (restricted/elevated/unknown), payload-level enforcement; success → artifact node + `StateUpdate`; context fusion into prompt; retry error-log injection; monitor-stop → FAILURE; **crash → structured FAILURE with traceback**; deadline overshoot flagged not failed; token budget exhaustion; token metrics; bus lifecycle (consume → publish to callback queue) |
| `mythos/orchestration/critic.py` | `tests/orchestration/test_critic_loop.py` | PASS → VALIDATED; FAIL → RETRY with verbatim error and same `task_id`; no-verdict **fails safe**; artifact read from matrix; mechanical `validation_command` (exit 0 / verbatim stderr + exit code); retry exhaustion escalates; SUCCESS-without-payload **fails closed**; crash conversion; `submit_verdict` tool authoritative; full worker↔critic fail-then-pass round trip over the bus |
| `mythos/orchestration/orchestrator.py`, `workflows.py` | `tests/orchestration/test_orchestrator.py` | Built-in workflow lookup; goal substitution + **shell-quoting of goal in validation commands**; literal steps; matrix seeding (system + goal nodes); sequential and **concurrent independent-step dispatch with join**; failure stops/blocks dependents; dispatched payload shape; `SwarmTimeoutError`; unmatched updates buffered not dropped |
| `mythos/orchestration/decomposer.py` | `tests/orchestration/test_decomposer.py` | Role prefilter keywords; strict JSON parsing (fenced JSON, unknown role, empty objective, step cap, `depends_on` forward/self refs); re-prompt carries parse error verbatim; double failure → configured fallback workflow; dynamic two-step e2e over `SwarmRuntime` |
| `mythos/orchestration/personas.py` | `tests/orchestration/test_personas.py` | Frontmatter parsing + rejection cases; system-suffix compilation; built-ins cover all roles; **override dir wins**; duplicate role rejected; persona reaches the worker system prompt |
| `mythos/orchestration/governor.py` | `tests/orchestration/test_governor.py` | Unlimited default; run budget trips; `reset_run` semantics; hourly window pruning (mocked clock); tripped governor refuses work **without constructing an LLM** |
| `mythos/orchestration/ledger.py` | `tests/orchestration/test_ledger.py` | Create/read round trip; stable node id across updates; `tracks` edge to goal node; out-of-range/missing errors; e2e dispatched → validated transitions |
| `mythos/orchestration/runtime.py` | `tests/orchestration/test_end_to_end_inmemory.py`, `test_decomposer.py`, `test_ledger.py`, `tests/integration/test_swarm_live.py` | Full swarm happy path writing a real file; retry-loop recovery; retries-exhausted failure; identical flow over live RabbitMQ + Qdrant |
| `mythos/orchestration/server.py`, `doctor.py`, `envfile.py` | `tests/test_pc_edition.py` | Env-file parsing/precedence (exported env wins), template written once; doctor FAIL on missing key / OK with key / report formatting / exit codes; control panel: dashboard page, status, **single** goal lifecycle incl. live ledger, bad-goal 400 / unknown-run 404 |
| `mythos/agent.py`, `executor.py` | `tests/test_agent.py`, `test_stuck_plan.py` | Full loop with StubLLM: finish, tool-then-finish, plain-text nudge, unknown tool recovery, iteration cap, stuck-plan deadlock report |
| `mythos/llm.py` | `tests/test_llm.py`, `test_llm_wire.py`, `test_llm_usage.py`, `test_providers_fake.py` | Provider abstraction; wire-format translation (tool-calling history); usage extraction, prompt caching, `RetryingLLM` backoff; Anthropic/OpenAI request assembly via fake SDK modules |
| `mythos/monitor.py`, `planner.py`, `memory.py`, `config.py` | `test_monitor.py`, `test_monitor_budget.py`, `test_planner.py`, `test_memory.py`, `test_config.py` | Monitor limits + real token/wall budgets; plan lifecycle; short/long-term memory; env-driven config defaults |
| `mythos/tools.py`, `tools_web.py`, `tools_geo.py`, `tools_tts.py` | `test_tools.py`, `test_hardening.py`, `test_tools_web.py`, `test_tools_geo.py`, `test_tools_tts.py` | Registry; calculator sandbox escapes + resource limits; shell exit/truncation/timeout coercion; **SSRF policy** on `web_fetch`; geo and TTS tools over mocked HTTP |

---

## 3. Gap Analysis

Each gap below was verified against the current test files; none is speculative.

**G1 — `RabbitMQBus` has zero unit-tier coverage; the heartbeat-during-long-handler path is untested anywhere.**
The driver's most intricate logic — running the handler on a side thread while the consumer thread pumps `conn.process_data_events()` to keep heartbeats alive, and the publisher reconnect-and-retry (`_reset_publisher`) — is exercised only indirectly by `tests/integration/test_rabbitmq_bus.py`, whose handlers return in milliseconds. No test (unit or integration) blocks a handler longer than the heartbeat interval, which is the exact failure mode the pattern exists to prevent. A unit test with a fake `pika` module (the `test_providers_fake.py` technique) could assert the ack-after-handler ordering, the nack/requeue decision, and that `process_data_events` is called while the handler runs.

**G2 — Worker shared-LLM reuse is untested.** Every worker test injects `llm_factory`, so `WorkerAgent._task_llm()`'s production branch — lazily building one `RetryingLLM(create_llm(...))` and caching it in `_shared_llm` across tasks — never executes. A regression that rebuilds the client per task (connection-pool churn) or cross-contaminates state would not be caught.

**G3 — No CLI-level tests for `main.py`.** Nothing in `tests/` imports `main`. Untested: `parse_args` validation (`--max-iterations` positivity), `build_config` override precedence (`--quiet` vs `--verbose`), `_build_orch_config`'s **`--dynamic` + `--workflow` fallback wiring** (`dynamic=True`, `fallback_workflow=args.workflow`), unknown-workflow exit code 2, and `SwarmTimeoutError` → exit 1.

**G4 — The interactive REPLs are untested.** Both `interactive_mode()` and the `--swarm` interactive session loop in `run_swarm()` (runtime persistence across goals, `exit`/EOF handling) have no coverage. A patched-`input` unit test would cover them cheaply.

**G5 — Control-panel concurrency untested.** `RunManager` is deliberately a serial queue, but `test_pc_edition.py::test_goal_lifecycle` submits exactly one goal. Two goals posted back-to-back (second shows `queued`, both complete in order), and `shutdown()` while a run is in flight, are unverified.

**G6 — Persona override precedence only partially covered.** `builtin_personas(override_dir)` precedence is tested directly, but the wiring `OrchestrationConfig.persona_dir` (env `MYTHOS_PERSONA_DIR`) → `SwarmRuntime` → per-role worker persona is not exercised end to end.

**G7 — No upgrade/migration story for persisted matrix data.** `QdrantDataMatrix` payloads carry no schema-version field, and no test loads a node written in an older payload shape. A field rename in `MemoryNode` would silently break persistent collections.

**G8 — No load/soak tier.** Nothing measures throughput, queue depth under many concurrent goals, memory growth of `InMemoryDataMatrix`/`_unmatched`, or long-lived RabbitMQ consumers over hours.

**G9 — Test-hygiene risks.** `test_worker.py::test_role_listing_unknown_tool_raises` mutates the module-global `roles.ROLE_TOOLS` directly (restored in `finally`, but a fragile pattern — prefer `monkeypatch.setitem`). Several tests also reach into privates (`matrix._nodes`, `bus._get`, `orchestrator._unmatched`), coupling the suite to implementation details.

*Checked and found covered (not gaps):* redelivery semantics on both bus drivers; critic fail-closed paths; orchestrator buffering of unmatched updates; shell-injection via goal text; dynamic-decomposition fallback; doctor exit codes.

---

## 4. Recommended Additions

| Pri | Item | Gap | Effort |
|---|---|---|---|
| **P1** | Fake-`pika` unit tests for `RabbitMQBus`: ack ordering, nack/requeue-once, `process_data_events` pumped during a slow handler, publisher reconnect retry | G1 | **M** |
| **P1** | Integration test: handler sleeps > heartbeat interval (short broker heartbeat via URL params), message still acked, consumer survives | G1 | **S** |
| **P1** | `main.py` CLI tests: `--dynamic`+`--workflow` fallback wiring, bad workflow → exit 2, timeout → exit 1, `--quiet`/`--verbose` precedence | G3 | **S** |
| **P1** | Worker shared-LLM test: two `handle()` calls with a counting fake `create_llm`; assert one construction, one `RetryingLLM` wrap | G2 | **S** |
| **P2** | Control panel: two goals queued (ordering, statuses), shutdown mid-run | G5 | **S** |
| **P2** | Runtime persona wiring: `persona_dir` override visible in a worker's system prompt through `SwarmRuntime` | G6 | **S** |
| **P2** | REPL tests with patched `stdin` for `interactive_mode` and the swarm session | G4 | **S** |
| **P2** | Replace direct `ROLE_TOOLS` mutation with `monkeypatch.setitem`; audit private-attribute access | G9 | **S** |
| **P3** | Version field on matrix payloads + forward-compat load test for legacy payload shape | G7 | **M** |
| **P3** | Nightly (non-blocking) soak: N concurrent goals over live RabbitMQ/Qdrant, assert completion, bounded memory and queue depth | G8 | **L** |

---

## 5. Release Quality Gates

A release candidate ships only when **all** of the following are green:

1. **Unit matrix:** `pytest tests/ -q` passes on Python 3.9, 3.10, 3.11, and 3.12 (CI `test` job, no skips beyond the documented `qdrant_client` importskip).
2. **Integration:** CI `integration` job passes against `rabbitmq:3.13` + `qdrant:v1.12.4` service containers with zero reachability skips.
3. **Stub e2e demo:** `python main.py --swarm --provider stub "<demo goal>"` and the equivalent `--dynamic` run complete without error on a clean checkout (manual or scripted smoke).
4. **Doctor clean:** `python main.py --doctor` exits 0 (no FAIL rows) on a machine with `ANTHROPIC_API_KEY` set; WARNs are triaged and documented.
5. **No new gaps:** any change to `bus.py`, `worker.py`, `critic.py`, or `schemas.py` lands with tests in the corresponding tier, and the Section 3 gap list is re-reviewed.
