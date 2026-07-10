# Package Structure — organized by concern

Mythos is laid out so each concern lives in one place. The flat module pile has
been grouped into packages; the previous top-level module paths remain
importable via thin aliases, so this reorganization is fully backward-compatible.

```
mythos/
├── __init__.py            # public API: MythosAgent, MythosConfig, __version__
│
│   ── core agent (Reason → Act → Observe) ──────────────────────────────
├── agent.py               # MythosAgent — the loop
├── executor.py            # one task's LLM ↔ tool ↔ result cycle
├── planner.py             # Plan / Task / Planner (DAG-ready ordering)
├── memory.py              # short-term window (FIFO eviction) + long-term KV
├── monitor.py             # caps, loop detection, token/wall budgets
├── llm.py                 # provider layer: Anthropic / OpenAI / Local / Stub / Retrying
├── config.py              # MythosConfig
│
│   ── cross-cutting primitives & governance ────────────────────────────
├── ordering.py            # BoundedFifo / BoundedLifo (see ORDERING.md)
├── guardrails.py          # protected-path + destructive-shell deny-lists
├── approvals.py           # human-in-the-loop gate for outward/destructive actions
│
│   ── tools (one package, one concern) ─────────────────────────────────
├── tools/
│   ├── __init__.py        # Tool, ToolRegistry, build_default_registry + core tools
│   ├── web.py             # web_fetch (SSRF-hardened)
│   ├── geo.py             # openrouteservice (navigator)
│   ├── tts.py / asr.py    # voice out / in
│   ├── assistant.py       # digital secretary (pa_*)
│   ├── computer.py        # computer use (open/clipboard/notify/screenshot + action loop)
│   └── browser.py         # web use (Playwright + web_fetch fallback)
│
│   ── PC edition ───────────────────────────────────────────────────────
├── pc/
│   ├── envfile.py         # ~/.mythos/env + ./.env loading  (mythos --init)
│   └── doctor.py          # environment diagnostics          (mythos --doctor)
│
│   ── multi-agent swarm ────────────────────────────────────────────────
└── orchestration/
    ├── schemas.py         # M2M contracts (TaskPayload / StateUpdate / MemoryNode)
    ├── bus.py             # message bus (RabbitMQ / InMemory) — FIFO transport
    ├── matrix.py          # Data Matrix (vector + graph, trust-ranked)
    ├── ingest.py          # knowledge-base ingestion
    ├── roles.py worker.py critic.py orchestrator.py   # the swarm
    ├── workflows.py decomposer.py ledger.py           # planning & progress
    ├── governor.py posture.py audit.py                # cost, health, replay
    ├── personas.py + personas/ + personas/library/    # role & specialist personas
    ├── events.py server.py                            # SSE + Boss control panel
    ├── scheduler.py                                   # proactive routines
    └── runtime.py                                     # wiring & lifecycle
```

## Backward compatibility

Every module moved into `tools/` or `pc/` keeps its old import path via a tiny
alias module that re-registers the real module in `sys.modules`, e.g.
`mythos/tools_web.py` → `mythos.tools.web`. So `from mythos.tools_web import
web_fetch`, `from mythos import tools_web`, and monkeypatching
`mythos.tools_web.<attr>` all behave exactly as before. New code should prefer
the package paths (`mythos.tools.web`, `mythos.pc.doctor`).

## Why the core agent stays flat

`agent`, `executor`, `planner`, `memory`, `monitor`, `llm`, `config` are the
stable, ubiquitously-imported heart of the framework. They already form one
cohesive concern at the package root; wrapping them in a `core/` subpackage
would churn hundreds of import sites (and `mythos.__init__`) for no real
clarity gain, so they are deliberately left at the top level.

See [ORDERING.md](ORDERING.md) for the FIFO/LIFO map and
[JARVIS_BLUEPRINT.md](JARVIS_BLUEPRINT.md) for the architecture.
