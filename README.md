# Mythos

**Mythos** is a small, dependency-light autonomous AI agent framework in Python.
You give it a goal; it plans, acts through tools, observes results, reflects,
and stops when the goal is achieved — a full Reason → Act → Observe loop with
self-monitoring.

```
User goal
   │
   ▼
MythosAgent.run(goal)
   ├── Planner   – tracks the goal as an ordered task list
   ├── Executor  – drives each task: LLM → tool call → result → LLM
   ├── Memory    – short-term message window + long-term key/value store
   ├── Monitor   – iteration caps, failure counters, loop detection, reflection
   └── Tools     – file I/O, shell, math, time, memory, finish
```

## Installation

```bash
git clone https://github.com/amjad2161/Mythos.git
cd Mythos
pip install -e .            # installs the anthropic SDK (default backend)
pip install -e ".[openai]"  # optional: OpenAI backend
pip install -e ".[dev]"     # optional: pytest for development
```

Requires Python 3.9+.

## Quick start

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py "Write a Python script that prints the Fibonacci sequence to /tmp/fib.py"
```

Or from Python:

```python
from mythos import MythosAgent

agent = MythosAgent()
result = agent.run("Research the top 3 Python web frameworks and write a comparison to /tmp/comparison.md")
print(result)
```

Run without a goal for an interactive prompt:

```bash
python main.py
```

Try it offline (no API key needed) with the deterministic stub backend:

```bash
python main.py --provider stub "smoke test"
```

Run it on a **free / local model** (Ollama, LM Studio, llama.cpp, vLLM, Groq) —
any OpenAI-compatible endpoint, no API key, no extra dependencies:

```bash
ollama serve && ollama pull llama3.1        # or any local server
python main.py --provider local --model llama3.1 "Write a haiku about the sea"
# point elsewhere with MYTHOS_LOCAL_URL=http://host:port/v1
```

Adopt a **specialist persona** from the bundled library for a single run:

```bash
python main.py --list-personas                       # 24 specialists
python main.py --persona engineering-backend-architect "Design a rate limiter"
```

## Engineering dossier

The full delivery documentation set lives in [`docs/`](docs/):

| Document | What it covers |
|---|---|
| [JARVIS_BLUEPRINT.md](docs/JARVIS_BLUEPRINT.md) | **The master A-Z blueprint** — the whole system unified into one JARVIS-class assistant (computer use, web use, secretary, singularity orchestration), with a built-vs-designed status matrix |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The three-layer design, M2M protocol, Data Matrix, roadmap |
| [PRD.md](docs/PRD.md) | Problem, personas, user journeys, FR/NFR requirements, KPIs, release criteria |
| [SECURITY.md](docs/SECURITY.md) | Trust boundaries, STRIDE threat model, permissions/access levels, hardening checklist |
| [OPERATIONS.md](docs/OPERATIONS.md) | Topology, config reference, health checks, runbook procedures, scaling |
| [QA.md](docs/QA.md) | Test architecture, coverage map, gap analysis, release quality gates |
| [PERFORMANCE.md](docs/PERFORMANCE.md) | Measured benchmarks (`scripts/bench.py`), capacity envelope, limits |
| [VISION_MAP.md](docs/VISION_MAP.md) | The full autonomous-agent vision mapped layer-by-layer onto the code |
| [ORDERING.md](docs/ORDERING.md) | End-to-end FIFO/LIFO/priority map of every queue, buffer, window, and eviction |
| [STRUCTURE.md](docs/STRUCTURE.md) | Package layout organized by concern (core / tools / pc / orchestration) |
| [JARVIS_ANALYSIS.md](docs/JARVIS_ANALYSIS.md) | Comparison vs Microsoft JARVIS/HuggingGPT, Leon AI; what to adopt/offer |

## Run it on your PC (one command)

```bash
./scripts/launch.sh          # Linux/macOS   (add --offline for no-docker mode)
.\scripts\launch.ps1         # Windows       (add -Offline for no-docker mode)
```

The launcher creates a virtualenv, installs Mythos, writes a config template
to `~/.mythos/env` (put your `ANTHROPIC_API_KEY` there — it and `./.env` are
loaded automatically), starts RabbitMQ + Qdrant if docker is available, runs
the environment doctor, and opens the **local web control panel** at
http://127.0.0.1:8642 — submit goals from the browser and watch each step of
the Task Ledger progress live.

Individual pieces:

```bash
python main.py --init      # write ~/.mythos/env config template
python main.py --doctor    # diagnose: API key, packages, RabbitMQ/Qdrant, voice/nav
python main.py --serve     # the web control panel (--port 8642 --host 127.0.0.1)
python main.py --swarm     # interactive swarm shell (goal after goal, shared memory)
python main.py --schedule knowledge/routines.example.json   # proactive routine daemon
```

## Multi-agent swarm (Phase A)

Mythos also runs as a **multi-agent system**: an orchestrator decomposes the
goal, routes strict JSON work orders over RabbitMQ to specialised workers,
a critic validates every result (autonomously retrying failed work with the
exact error output), and all shared knowledge lives in a Qdrant-backed
"Data Matrix" (vector search + knowledge graph). Full design:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```bash
pip install -e ".[orchestration]"   # pika + qdrant-client + fastembed
docker compose up -d                # RabbitMQ (5672) + Qdrant (6333)

export ANTHROPIC_API_KEY=sk-ant-...
python main.py --swarm "Write a Python script that prints the Fibonacci sequence to /tmp/fib.py"
```

Everything also runs offline with in-memory drivers (no Docker, no API key):

```bash
python main.py --swarm --provider stub --bus inmemory --matrix inmemory "demo"
```

(In stub mode the critic cannot obtain a real verdict, so this demonstrates
the fail-safe path: three autonomous retries, then a reported failure.)

**Dynamic orchestration (Phase B)** — instead of a rigid workflow, a cheap
routing model decomposes the goal into role-assigned steps (strict JSON,
deterministic fallback on parse failure):

```bash
python main.py --swarm --dynamic "Plan a driving route from Haifa to Eilat and narrate it as audio"
```

Specialised roles and their services:

| Role | Tools | Needs |
|---|---|---|
| `backend_dev` | files, shell, calculate | — |
| `researcher` | SSRF-hardened `web_fetch` (no shell) | network egress |
| `navigator` | `ors_geocode/directions/isochrones/matrix` | `ORS_API_KEY` ([free key](https://openrouteservice.org)) or self-hosted `MYTHOS_ORS_URL` |
| `voice` | `speak` → OpenAI-compatible TTS sidecar | `MYTHOS_TTS_URL` (e.g. `docker compose --profile voice up` — supertonic: MIT code, OpenRAIL-M model weights) |
| `assistant` | digital secretary: `pa_add_task/list_tasks/complete_task`, `pa_add_note/list_notes`, `pa_set_reminder/due_reminders`, `pa_draft_email`, `pa_daily_brief` | local JSON store (`MYTHOS_ASSISTANT_DIR`, default `~/.mythos/assistant`) — offline |
| `operator` | computer use: `open_url`, `open_path`, `clipboard_get/set`, `notify`, `screenshot` (no shell) | OS backends where present (xdg-open, xclip/wl-clip, notify-send, mss/scrot); degrades gracefully |
| `browser` | web use: `browser_navigate`, `browser_read_page` (indexed DOM), `browser_click`, `browser_fill`, `browser_screenshot` (no shell) | Playwright + Chromium (`pip install playwright && playwright install chromium`); degrades to read-only `web_fetch` when absent |

Every role carries a Markdown **persona** (override with `MYTHOS_PERSONA_DIR`);
token spend is metered for real (`LLMResponse.usage` → Monitor budgets +
prompt caching) and governed by an hourly/run **cost circuit breaker**
(`MYTHOS_HOURLY_TOKEN_BUDGET`, `MYTHOS_RUN_TOKEN_BUDGET`); each goal keeps a
durable **task ledger** in the Data Matrix.

Integration tests against live services:

```bash
docker compose up -d
python -m pytest tests/integration -m integration
```

## Knowledge base

Feed the swarm curated domain knowledge as **ground truth**. A hierarchical
taxonomy or outline (Markdown headings or numbered sections) is parsed into
graph-linked nodes in the Data Matrix — a KB root, a `kb_category` per section,
and a `kb_topic` per line — stored verbatim at reference trust. Agents then
`navigate` the matrix, land on a relevant topic, and traverse its
`belongs_to`/`part_of` edges up to the broader domain for context.

```bash
# Ingest the bundled seed taxonomy into a persistent Qdrant matrix
docker compose up -d
python main.py --ingest knowledge/agent_project_kb.md --matrix qdrant

# Ingest-then-query in one offline run (in-memory, no services)
python main.py --ingest knowledge/agent_project_kb.md \
               --kb-query "autonomous agents RAG" --matrix inmemory
```

`knowledge/agent_project_kb.md` ships a 12-domain agent-development taxonomy
(12 categories, 62 topics). Point `--ingest` at any outline of your own; use
`--kb-name` to label it.

## Configuration

Everything is configurable via `MythosConfig`, CLI flags, or environment variables:

| Environment variable        | Default                | Meaning |
|-----------------------------|------------------------|---------|
| `ANTHROPIC_API_KEY`         | —                      | API key for the default Claude backend |
| `MYTHOS_API_KEY`            | —                      | Overrides the API key for any provider |
| `MYTHOS_LLM_PROVIDER`       | `anthropic`            | `anthropic` \| `openai` \| `local` \| `stub` |
| `MYTHOS_LOCAL_URL`          | `http://localhost:11434/v1` | OpenAI-compatible base URL for `--provider local`/`ollama` (Ollama, LM Studio, llama.cpp, vLLM, Groq) |
| `MYTHOS_LOCAL_API_KEY`      | `local`                | Bearer token for the local endpoint (most local servers ignore it) |
| `MYTHOS_LLM_MODEL`          | `claude-opus-4-8`      | Model ID |
| `MYTHOS_LLM_MAX_TOKENS`     | `8192`                | Max output tokens per LLM call |
| `MYTHOS_LLM_TEMPERATURE`    | `0.2`                  | Sampling temperature (OpenAI backend only; current Claude models don't accept it) |
| `MYTHOS_MAX_ITERATIONS`     | `50`                   | Hard cap on autonomous iterations |
| `MYTHOS_MAX_FAILURES`       | `5`                    | Consecutive failures before the monitor stops the run |
| `MYTHOS_REFLECTION_INTERVAL`| `5`                    | Inject a self-reflection checkpoint every N iterations |
| `MYTHOS_MEMORY_WINDOW`      | `20`                   | Recent messages kept in the LLM context |
| `MYTHOS_PERSIST_MEMORY`     | `false`                | Persist long-term memory to disk |
| `MYTHOS_MEMORY_PATH`        | `mythos_memory.json`   | Long-term memory file |
| `MYTHOS_VERBOSE`            | `true`                 | Set `false` to silence progress output |
| `MYTHOS_AUDIT_LOG`          | —                      | Path to a JSONL audit log; when set, every swarm lifecycle event is durably recorded for deterministic replay (`orchestration.audit.replay`) |
| `MYTHOS_APPROVALS`          | `off`                  | Set `on` to require human approval for outward/destructive tool calls (register an approver via `mythos.approvals.set_approver`); `MYTHOS_AUTO_APPROVE=on` allows them unattended |

CLI flags (`--provider`, `--model`, `--api-key`, `--max-iterations`, `--quiet`, …) override
environment variables. See `python main.py --help`.

## Built-in tools

`current_time`, `calculate`, `read_file`, `write_file`, `append_file`,
`list_directory`, `run_shell`, `memory_store`, `memory_recall`, `memory_list`,
and `finish` (the agent calls `finish` to end the run with its conclusion).

Register your own:

```python
from mythos import MythosAgent
from mythos.tools import Tool

def greet(name: str) -> str:
    return f"Hello, {name}!"

agent = MythosAgent()
agent.add_tool(Tool(
    name="greet",
    description="Greet a person by name.",
    parameters={"name": {"type": "string", "description": "Person to greet."}},
    func=greet,
    required=["name"],
))
```

## Safety notes

- `run_shell` executes arbitrary shell commands **by design** — run the agent in a
  sandbox/container if you don't fully trust the goal or model output.
- The monitor enforces an iteration cap, a consecutive-failure cap, and repetitive-call
  (infinite-loop) detection as guardrails.

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

## License

MIT
