# Mythos Operations Runbook

**Audience:** operators running a local Mythos multi-agent install.
**Scope:** the control panel (`mythos --serve`), the Phase A/B swarm, and its Docker infrastructure.
**Source of truth:** this document is derived from the code (`mythos/config.py`, `mythos/orchestration/*`, `scripts/launch.*`, `docker-compose.yml`); when in doubt, the code wins.

---

## 1. Topology

A standard local install is **one Python process plus two (optionally three) containers**.

### 1.1 Processes and threads

```
python main.py --serve                     (single process, 127.0.0.1:8642)
 ├─ ThreadingHTTPServer          – dashboard + JSON API (thread per request)
 ├─ run-manager thread           – serial goal queue; owns one shared SwarmRuntime
 └─ SwarmRuntime (created lazily on the FIRST submitted goal)
     ├─ orchestrator thread      – consumes q.orchestrator.results
     ├─ critic thread            – consumes q.critic.review
     └─ worker-<role> threads    – ONE consumer thread per role, consuming q.tasks.<role>
```

- **Rigid mode** (default workflow `code_delivery`): only the workflow's roles get workers.
- **Dynamic mode** (`--dynamic` / `MYTHOS_DYNAMIC=true`): every known role except `critic` gets a worker — `backend_dev`, `researcher`, `navigator`, `voice`.
- With the RabbitMQ bus, each consumer thread runs its message handler in a **side thread** while it keeps pumping AMQP heartbeats — a worker busy on a long LLM task does not lose its broker connection.
- All agent threads are daemons. Every agent boundary is a real bus message, so the single-process layout is a deployment choice, not an architectural one (see §6).

### 1.2 Containers and ports

| Service | Image | Port(s) | Purpose |
|---|---|---|---|
| Control panel | (host process) | **8642** (127.0.0.1) | Dashboard + `/api/*` |
| RabbitMQ | `rabbitmq:3.13-management` | **5672** AMQP, **15672** mgmt UI | Message bus (`q.tasks.<role>`, `q.critic.review`, `q.orchestrator.results`) |
| Qdrant | `qdrant/qdrant:v1.12.4` | **6333** HTTP, 6334 gRPC | Data Matrix (collection `mythos_matrix`, volume `qdrant_data`) |
| supertonic (optional) | `python:3.12-slim` + pip | **8000** | OpenAI-compatible TTS for the `voice` role (`docker compose --profile voice up`) |

### 1.3 Startup order

1. `docker compose up -d` — RabbitMQ and Qdrant (both have healthchecks; Qdrant is ready when `GET /readyz` returns 200).
2. `python main.py --doctor` — verify environment.
3. `python main.py --serve` — panel comes up immediately; the swarm (bus/matrix connections, worker threads) starts **lazily on the first submitted goal**. Submitting a goal before the containers are healthy fails that run, not the server.

---

## 2. Installation

### 2.1 One-command launcher (recommended)

```bash
./scripts/launch.sh              # Linux/macOS
.\scripts\launch.ps1             # Windows PowerShell
```

The launcher: creates `.venv`, runs `pip install -e ".[orchestration]"` (falls back to plain `-e .`), writes the config template via `main.py --init`, runs `docker compose up -d` if docker exists (otherwise drops to in-memory backends), runs `--doctor`, then execs `--serve`. Add `--offline` (bash) / `-Offline` (PowerShell) to force `--bus inmemory --matrix inmemory`.

### 2.2 Manual

```bash
git clone https://github.com/amjad2161/Mythos.git && cd Mythos
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[orchestration]"        # pika + qdrant-client + fastembed
python main.py --init                    # writes ~/.mythos/env template
# put ANTHROPIC_API_KEY=sk-ant-... into ~/.mythos/env
docker compose up -d
python main.py --doctor
python main.py --serve                   # --host 127.0.0.1 --port 8642 are the defaults
```

Alternative entry points: `python main.py --swarm "goal"` (one-shot CLI swarm run), `python main.py --swarm` (interactive shell), `python main.py "goal"` (single-agent mode, no swarm infra needed).

### 2.3 Offline / in-memory mode (no Docker, no API key)

```bash
python main.py --serve --bus inmemory --matrix inmemory
python main.py --swarm --provider stub --bus inmemory --matrix inmemory "demo"
```

Note: with `--provider stub` the critic cannot produce a real verdict, so runs exercise the fail-safe path (3 attempts, then reported failure). This is expected, not a defect.

---

## 3. Configuration reference

Config is loaded from, in order of precedence: **exported environment > `~/.mythos/env` > `./.env`** (env files never override already-set variables; see `mythos/envfile.py`). CLI flags override everything.

### 3.1 LLM backend (`mythos/config.py`)

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | API key for the default Claude backend |
| `MYTHOS_API_KEY` | — | Overrides the API key for any provider |
| `MYTHOS_LLM_PROVIDER` | `anthropic` | `anthropic` \| `openai` \| `stub` |
| `MYTHOS_LLM_MODEL` | `claude-opus-4-8` | Model ID |
| `MYTHOS_LLM_MAX_TOKENS` | `8192` | Max output tokens per call |
| `MYTHOS_LLM_TEMPERATURE` | `0.2` | OpenAI backend only; Anthropic backend ignores it |

### 3.2 Agent loop & memory (`mythos/config.py`)

| Variable | Default | Meaning |
|---|---|---|
| `MYTHOS_MAX_ITERATIONS` | `50` | Hard cap on autonomous iterations per agent run |
| `MYTHOS_MAX_FAILURES` | `5` | Consecutive failures before the monitor stops |
| `MYTHOS_REFLECTION_INTERVAL` | `5` | Self-reflection checkpoint every N iterations |
| `MYTHOS_MAX_TOTAL_TOKENS` | `0` | Per-run cumulative token budget (0 = unlimited) |
| `MYTHOS_MAX_WALL_SECONDS` | `0` | Per-run wall-clock deadline (0 = unlimited) |
| `MYTHOS_MEMORY_WINDOW` | `20` | Recent messages kept in LLM context |
| `MYTHOS_PERSIST_MEMORY` | `false` | Persist long-term memory to disk |
| `MYTHOS_MEMORY_PATH` | `mythos_memory.json` | Long-term memory file |
| `MYTHOS_VERBOSE` | `true` | `false` silences progress output |

### 3.3 Swarm infrastructure (`mythos/orchestration/config.py`)

| Variable | Default | Meaning |
|---|---|---|
| `MYTHOS_BUS` | `rabbitmq` | Bus backend: `rabbitmq` \| `inmemory` |
| `MYTHOS_MATRIX` | `qdrant` | Data Matrix backend: `qdrant` \| `inmemory` |
| `MYTHOS_BROKER_URL` | `amqp://mythos:mythos@localhost:5672/` | AMQP connection URL |
| `MYTHOS_QDRANT_URL` | `http://localhost:6333` | Qdrant HTTP URL |
| `MYTHOS_MATRIX_COLLECTION` | `mythos_matrix` | Qdrant collection name |
| `MYTHOS_EMBEDDER` | `fastembed` | `fastembed` (local ONNX) \| `hash` (deterministic, no download) |

### 3.4 Orchestration behaviour

| Variable | Default | Meaning |
|---|---|---|
| `MYTHOS_MAX_ATTEMPTS` | `3` | Attempts per subtask before the critic reports FAILURE |
| `MYTHOS_LLM_RETRY_ATTEMPTS` | `3` | Exponential-backoff retries on transient LLM errors |
| `MYTHOS_LLM_RETRY_BASE_S` | `1.0` | Backoff base (seconds) |
| `MYTHOS_DYNAMIC` | `false` | Enable Phase B LLM-driven decomposition |
| `MYTHOS_DECOMPOSER_MODEL` | `claude-haiku-4-5` | Cheap routing model for decomposition |
| `MYTHOS_DECOMPOSER_MAX_STEPS` | `6` | Max steps in a dynamic plan |
| `MYTHOS_FALLBACK_WORKFLOW` | `code_delivery` | Rigid workflow / dynamic-parse-failure fallback |
| `MYTHOS_RESULT_TIMEOUT_S` | `0` | Per-subtask wait for a validated result; `0` = auto (see §5.3) |
| `MYTHOS_ORCHESTRATOR_ID` | `orchestrator-0` | Node id stamped on issued TaskPayloads |
| `MYTHOS_PERSONA_DIR` | `''` | Directory of Markdown persona overrides |

### 3.5 Cost governance

| Variable | Default | Meaning |
|---|---|---|
| `MYTHOS_HOURLY_TOKEN_BUDGET` | `0` | Sliding 60-min window budget, all runs (0 = unlimited) |
| `MYTHOS_RUN_TOKEN_BUDGET` | `0` | Per-goal budget, reset at each goal start (0 = unlimited) |

### 3.6 Domain agents

| Variable | Default | Meaning |
|---|---|---|
| `ORS_API_KEY` | — | openrouteservice key for the `navigator` role |
| `MYTHOS_ORS_URL` | `https://api.openrouteservice.org` | Override for self-hosted ORS |
| `MYTHOS_TTS_URL` | — | OpenAI-compatible TTS base URL for the `voice` role (e.g. `http://localhost:8000`) |
| `MYTHOS_TTS_MODEL` | `supertonic` | TTS model name sent to the sidecar |

Roles without their service configured **refuse tasks** rather than fabricate results.

---

## 4. Health & observability

| Check | How | Healthy looks like |
|---|---|---|
| Environment | `python main.py --doctor` (exit code 1 on any FAIL) | `Ready. N optional capability warning(s).` — FAILs (Python <3.9, missing API key) block; WARNs are optional capabilities |
| RabbitMQ | Management UI `http://localhost:15672` (user `mythos` / pass `mythos`) | Connections present; `q.tasks.*` queue depths near 0 |
| Qdrant | `curl http://localhost:6333/readyz`; `curl http://localhost:6333/collections` | 200; collection `mythos_matrix` listed |
| Panel/swarm | `curl http://127.0.0.1:8642/api/status` | `{"started": true, "bus": "rabbitmq", "matrix": "qdrant", "roles": [...], "tokens_last_hour": N, ...}` |
| Runs | `GET /api/runs`, `GET /api/runs/<id>` | Per-run status: `queued → running → completed/failed` |
| TTS sidecar | `curl http://localhost:8000/v1/audio/speech` reachable | port open |

**Task Ledger — the per-goal source of truth.** Each goal gets a durable `ledger` node in the Data Matrix, updated only by the orchestrator, with one entry per step: `index, role, objective, task_id, status (pending/dispatched/validated/failed), attempts, summary`. It survives context resets and is what the dashboard renders live; `GET /api/runs/<id>` returns it under `"ledger"`. When diagnosing any run, read the ledger first.

**Logs.** Everything goes to **stdout** of the `--serve`/`--swarm` process (there are no log files); the HTTP handler is deliberately silent. `MYTHOS_VERBOSE=false` (or `--quiet`) silences agent progress lines. Container logs: `docker compose logs -f rabbitmq qdrant`.

**Token spend.** `tokens_last_hour` in `/api/status` is the governor's live sliding-window total — the primary cost signal.

---

## 5. Runbook procedures

### 5.1 Cold start
1. `cd Mythos && source .venv/bin/activate`
2. `docker compose up -d` — wait until `docker compose ps` shows both services healthy.
3. `python main.py --doctor` — resolve any FAIL before continuing.
4. `python main.py --serve` — open http://127.0.0.1:8642, submit a trivial goal to confirm end-to-end.

### 5.2 Clean shutdown
1. `Ctrl-C` in the `--serve` terminal. The server closes, then the RunManager shuts the runtime down: every agent gets a stop signal first, then joins (~5 s each max), then bus/matrix connections close.
2. `docker compose down` (add `-v` **only** if you intend to destroy the Data Matrix — see §5.6).
3. Note: **queued runs are lost** on shutdown — the run registry is in-memory. Completed artifacts and ledgers persist in Qdrant.

### 5.3 Goal stuck / `SwarmTimeoutError`
Symptom: a run fails with `SwarmTimeoutError: No validated result for task(s) [...] within Ns`, or a ledger step sits at `dispatched`.
1. `GET /api/runs/<id>` — find which step/role stalled and its attempt count.
2. Check the RabbitMQ UI: messages piling up in `q.tasks.<role>` mean the worker isn't consuming (see §5.4); a growing `q.critic.review` means the critic stalled; empty queues with no result usually mean a very long LLM/tool call.
3. Understand the window: with `MYTHOS_RESULT_TIMEOUT_S=0` (default) it is auto-derived as `max_attempts × (task timeout_ms/1000 + 150 s)` — with defaults, 3 × (300 + 150) = **1350 s** per subtask. Raise `MYTHOS_RESULT_TIMEOUT_S` for legitimately long tasks.
4. Timeouts are **cooperative**: the orchestrator gives up waiting but does not kill the in-flight worker. Its late result is buffered as an unmatched update; a restart of the serve process fully clears state.

### 5.4 Worker thread died
Symptoms: one role's steps stay `dispatched` forever; `q.tasks.<role>` depth grows in the RabbitMQ UI; the role's consumer connection is missing from the UI's Connections tab; a traceback on stdout.
1. Confirm via the RabbitMQ UI (Queues → consumers count = 0 for that queue).
2. There is **no in-process supervisor** — worker threads are not auto-restarted. Restart the `--serve` process (§5.2 then §5.1, containers can stay up).
3. Messages are durable and manually acked, so an unacked task is redelivered when the worker reconnects (once; a second handler crash drops it — check stdout for `dropped after redelivery`).

### 5.5 Cost governor tripped
Symptoms: steps fail fast with `COST_GOVERNOR_TRIPPED: hourly|run token budget exhausted (spent/budget)` in the ledger summary/error log.
1. Check spend: `GET /api/status` → `tokens_last_hour`; compare with `MYTHOS_HOURLY_TOKEN_BUDGET` / `MYTHOS_RUN_TOKEN_BUDGET`.
2. **Run budget**: resets automatically at the start of the next goal — re-submit, or raise `MYTHOS_RUN_TOKEN_BUDGET` and restart.
3. **Hourly budget**: a sliding 60-minute window — either wait for spend to age out, or raise the budget / set it to `0` and restart the serve process. The governor is in-memory; a restart also zeroes the window.
4. In-flight work is never interrupted; the governor only refuses **new** tasks.

### 5.6 Resetting the Data Matrix
- Surgical (keep the container): `curl -X DELETE http://localhost:6333/collections/mythos_matrix` — it is re-created on the next run. Do this with the swarm idle.
- Full wipe: `docker compose down && docker volume rm mythos_qdrant_data && docker compose up -d` (volume name = `<project-dir>_qdrant_data`; confirm with `docker volume ls`).
- This destroys all ledgers, artifacts, and knowledge-graph nodes. It does not touch RabbitMQ or `~/.mythos/env`.

### 5.7 Upgrading
```bash
git pull
source .venv/bin/activate
pip install -e ".[orchestration]"
docker compose up -d        # picks up any image tag changes
python main.py --doctor     # must report Ready before restarting --serve
```
Restart the serve process last. CI (`.github/workflows/ci.yml`) runs unit tests on Python 3.9–3.12 and integration tests against live RabbitMQ/Qdrant; mirror it locally with `python -m pytest tests/ -q` and `python -m pytest tests/integration -m integration`.

### 5.8 Backup
Stateful pieces (everything else is disposable):

| Item | Location | How |
|---|---|---|
| Data Matrix (ledgers, artifacts, knowledge) | Docker volume `qdrant_data` | Stop writes, then `docker run --rm -v mythos_qdrant_data:/data -v $PWD:/backup alpine tar czf /backup/qdrant.tgz /data`, or use Qdrant's snapshot API |
| Configuration & API keys | `~/.mythos/env` (plus any `./.env`) | Copy the file (contains secrets — store securely) |
| Single-agent long-term memory | `mythos_memory.json` (only if `MYTHOS_PERSIST_MEMORY=true`) | Copy the file |

RabbitMQ queues are transient job traffic; they are not worth backing up.

---

## 6. Capacity & scaling

- **The panel executes runs serially.** One RunManager thread, one shared SwarmRuntime; concurrent goal submissions queue (`status: queued`). Result correlation in the orchestrator is per-goal, so do not attempt to parallelize runs inside one process.
- **One worker thread per role** with `prefetch_count=1` — per-role concurrency is exactly 1. Two `backend_dev` steps cannot overlap; a dynamic plan alternating roles gives natural (but still step-serial) pipelining.
- **The LLM dominates latency.** Each step is an inner agent loop of LLM calls; bus and Qdrant round-trips are milliseconds. Throughput levers, in order: prompt caching + a cheaper `MYTHOS_DECOMPOSER_MODEL`, fewer steps (`MYTHOS_DECOMPOSER_MAX_STEPS`), lower `MYTHOS_MAX_ITERATIONS`, and only then horizontal scaling.
- **Scaling out is a deployment change, not a code change.** Every agent boundary is already a durable bus message on named queues. To split agents into processes/containers: run additional processes that construct only the desired `WorkerAgent`/`CriticAgent` against the same `MYTHOS_BROKER_URL` and `MYTHOS_QDRANT_URL`. Multiple workers on one `q.tasks.<role>` queue load-balance automatically via AMQP. Keep exactly **one orchestrator** per goal, and note the CostGovernor and Task Ledger writer are per-process (the ledger is single-writer by design).

---

## 7. Known operational limitations

| Limitation | Consequence | Mitigation |
|---|---|---|
| Cooperative timeouts only | `SwarmTimeoutError` abandons a wait but never kills the in-flight worker LLM call; late results are buffered, and spend continues until the step ends | Set token budgets; restart the serve process to hard-stop |
| Control panel has no auth or TLS | Anyone who can reach the port can run arbitrary goals (workers have `run_shell`) | Keep the default `127.0.0.1` bind; never `--host 0.0.0.0` on a shared network |
| RabbitMQ default creds (`mythos`/`mythos`), ports published to the host | Local users can access the broker and mgmt UI | Acceptable for single-user machines; change creds + `MYTHOS_BROKER_URL` otherwise |
| Single-process swarm | A process crash takes orchestrator, critic, and all workers down; queued panel runs (in-memory registry) are lost | Ledgers/artifacts persist in Qdrant; restart and re-submit |
| No thread supervision | A dead consumer thread is not restarted; its queue silently backs up | Monitor queue depths in the mgmt UI; restart the process (§5.4) |
| At-least-once delivery, bounded | A message whose handler crashes twice is dropped (logged to stdout) | Watch stdout for `dropped after redelivery` |
| Stub provider cannot satisfy the critic | Offline/stub runs always end as reported failures after `MYTHOS_MAX_ATTEMPTS` | Expected; use stub mode for wiring tests only |
| Governor and run history are in-memory | Restart zeroes hourly spend tracking and the run list | Treat provider-side billing as the authoritative spend record |
