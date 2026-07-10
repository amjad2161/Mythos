# Mythos — The JARVIS Blueprint

**One master architecture, A → Z.** This document unifies everything Mythos is
today into a single design for a JARVIS-class autonomous system: a personal
assistant / digital secretary with computer use, web use, long-term memory, and
an always-on orchestrating "brain" — built to talk to the user in real time,
both directions, and to act on their behalf under strict governance.

It is grounded in the actual codebase (every component named here exists unless
marked **[designed]** or **[roadmap]**) and in a survey of the state of the art
(Anthropic computer-use, OpenAI Operator/CUA, browser-use, Playwright MCP,
Agent-S, UI-TARS, HuggingGPT/JARVIS). Companion docs:
[ARCHITECTURE](ARCHITECTURE.md) (the three-tier design in depth),
[SECURITY](SECURITY.md) (threat model), [VISION_MAP](VISION_MAP.md) (vision →
code), [JARVIS_ANALYSIS](JARVIS_ANALYSIS.md) (comparative survey).

Legend: **[built]** shipped & tested · **[designed]** spec'd here, not yet coded
· **[roadmap]** future phase.

---

## 0. What Mythos is now (executive summary)

Mythos is two layers that share one tool/memory substrate:

1. **A single autonomous agent** (`mythos/`) — a full Reason → Act → Observe
   loop (Planner, Executor, Memory, Monitor, Tools) that pursues one goal.
2. **A multi-agent swarm** (`mythos/orchestration/`) — an Orchestrator ("Agent
   Boss") decomposes a goal, routes strict-JSON work orders over a message bus
   to **role-specialised workers**, a **Critic** validates every result
   (autonomously retrying failures with the verbatim error), and all shared
   knowledge lives in a **Data Matrix** (vector search fused with a knowledge
   graph) that now also holds an ingested **Knowledge Base**.

On top of that substrate this blueprint adds the JARVIS capability set: a
**digital secretary** (`assistant` role — tasks/notes/reminders/drafts/
briefings, **[built]**), **computer use** (`operator` role — open/clipboard/
notify/screenshot, **[built]** seam), **web use** (`researcher` + SSRF-hardened
`web_fetch` **[built]**; a Playwright `browser` role **[designed]**), and an
always-on **Boss front-end + Scheduler** **[designed]** that make it feel like a
single, responsive, ever-present assistant.

The design stance throughout: **dependency-light, stdlib-first, degrades
gracefully offline**, and **every new capability is a new role + tool module**
that plugs into the existing registry/access-level machinery — never a rewrite.

---

## 1. Component atlas (A → Z)

The whole system, by file, so the rest of the document can refer to real names.

### Core agent — `mythos/`
| Module | Responsibility |
|---|---|
| `agent.py` | `MythosAgent` — the R→A→O loop; owns Planner/Executor/Memory/Monitor; `last_run_ok`/`last_halt_reason`. |
| `planner.py` | `Plan`, `Task`, `Planner` — ordered task list with dependencies and progress. |
| `executor.py` | `Executor.run_task` — one task's LLM ↔ tool ↔ result loop; handles `finish`, reflection, errors. |
| `memory.py` | `Memory` = short-term sliding window (tool-exchange-integrity eviction) + long-term JSON KV. |
| `monitor.py` | `Monitor` — iteration cap, failure streak, loop detection, reflection, **token budget**, wall-clock. |
| `llm.py` | `BaseLLM` + `AnthropicLLM`/`OpenAILLM`/`StubLLM`/`RetryingLLM`; `LLMResponse.usage`; prompt caching. |
| `config.py` | `MythosConfig` (+ `from_env`). |
| `tools.py` | `Tool`, `ToolRegistry`, `build_default_registry`; core tools + safe calculator + shell/file/guardrail hooks. |
| `tools_web.py` | `web_fetch` — SSRF-hardened HTTP GET (scheme/host allowlist, per-hop re-validation, body cap). |
| `tools_geo.py` | `ors_*` — openrouteservice geocode/directions/isochrones/matrix. |
| `tools_tts.py` / `tools_asr.py` | `speak` / `transcribe` — voice out/in via OpenAI-compatible sidecars. |
| `tools_assistant.py` **[built]** | `pa_*` — secretary: tasks, notes, reminders, e-mail **drafts**, daily briefing (local JSON store). |
| `tools_computer.py` **[built]** | `open_url`, `open_path`, `clipboard_get/set`, `notify`, `screenshot` — computer-use seam. |
| `guardrails.py` | Filesystem/shell deny-lists (`check_path`, `check_shell`). |
| `envfile.py` / `doctor.py` | PC edition: `~/.mythos/env` loading; environment diagnostics. |

### Orchestration swarm — `mythos/orchestration/`
| Module | Responsibility |
|---|---|
| `schemas.py` | M2M contracts: `TaskPayload`, `StateUpdate`, `MemoryNode`, `Constraints`, trust constants. |
| `bus.py` | `MessageBus` + `InMemoryBus`/`RabbitMQBus`; queues `q.tasks.<role>`, `q.critic.review`, `q.orchestrator.results`. |
| `matrix.py` | `DataMatrix` + `InMemoryDataMatrix`/`QdrantDataMatrix`; `navigate()` = KNN + edge traversal + trust fusion. |
| `roles.py` | `ROLE_TOOLS` allow-lists; `build_registry_for_role`; `ACCESS_LEVELS`, `_MUTATING_TOOLS`. |
| `worker.py` | `WorkerAgent` — wraps `MythosAgent` per role; governor gate; crash→FAILURE; artifact upsert. |
| `critic.py` | `CriticAgent` — mechanical `validation_command` → LLM `submit_verdict`; retry with verbatim error. |
| `orchestrator.py` | `Orchestrator` — decompose → DAG dispatch (`_wait_for_any`, `depends_on`) → collect; emits events. |
| `workflows.py` | `Workflow`/`WorkflowStep`; built-ins `code_delivery`, `route_plan`. |
| `decomposer.py` | `DynamicDecomposer` — keyword prefilter → cheap LLM → strict-JSON steps → deterministic fallback. |
| `ledger.py` | `TaskLedger` — durable per-goal progress node. |
| `governor.py` | `CostGovernor` — hourly + per-run token circuit breaker. |
| `personas.py` + `personas/*.md` | Markdown personas compiled into each role's system suffix. |
| `events.py` + `server.py` | `EventHub` fan-out + SSE `/api/events`; stdlib web control panel. |
| `ingest.py` **[built]** | Knowledge-base ingestion: taxonomy → graph-linked `kb_*` MemoryNodes. |
| `runtime.py` | `SwarmRuntime` — wires bus/matrix/governor/events/workers/critic/orchestrator + lifecycle. |

### Roles today (`ROLE_TOOLS`)
`backend_dev`, `critic`, `researcher`, `navigator`, `voice`, **`assistant`
[built]**, **`operator` [built]**. Every role carries a persona; the critic is
structural (never dispatched directly).

---

## 2. The layered brain (target topology)

Today the Orchestrator is invoked per goal. The JARVIS target adds a
conversational front-end and an always-on scheduler around the same swarm, so
the system is responsive *and* proactive:

```
            ┌──────────────────────────────────────────────┐
  user ◀──▶ │  BOSS — conversational front-end (fast model) │  [designed]
   voice    │  intent routing · steering · HITL previews ·  │  streaming,
   text     │  progress narration                            │  interruptible
            └───────┬──────────────────────────┬────────────┘
                    │ delegate                  │ read state
                    ▼                           ▼
        ┌───────────────────────┐   ┌───────────────────────────┐
        │ Orchestrator + workers│   │ Long-term memory           │
        │ backend_dev · research│   │  vector: Qdrant Data Matrix │  [built]
        │ navigator · voice ·   │   │  graph: entity/edge store   │  [designed]
        │ assistant · operator  │   │  KB: ingested taxonomies    │  [built]
        │ + Critic  (RabbitMQ)  │   └───────────────────────────┘
        └───────────┬───────────┘               ▲
                    ▲                            │
                    │        ┌───────────────────┘
          ┌─────────┴────────┴────┐
          │ SCHEDULER daemon       │  routines · triggers · watchdogs  [designed]
          └────────────────────────┘
```

- **Boss** does not do heavy work; it classifies intent, holds dialogue state,
  decides *reactive-now* vs *delegate-to-background*, renders human-in-the-loop
  previews, and streams. This split is what makes a long task feel responsive.
- **Workers** are the existing role fleet consuming `q.tasks.<role>`; results
  flow back through the Critic. Long tasks run async; Boss narrates from the
  ledger + `EventHub`.
- **Scheduler** is the always-on heartbeat: fires routines (07:00 briefing,
  "15 min before a meeting pull the agenda") and watchdogs onto the bus.

---

## 3. Layer-by-layer specification

### 3.1 L0 — the autonomous agent (Reason → Act → Observe) **[built]**
`MythosAgent.run(goal)` seeds a plan and a system prompt, then loops: the
Executor asks the LLM, dispatches the requested tool, feeds the result back, and
the Monitor guards the loop (iteration cap, consecutive-failure cap, repetitive-
call loop detection, periodic reflection, **real token budget** from
`LLMResponse.usage`, optional wall-clock). The agent ends when the model calls
`finish`, the plan deadlocks, or a guard halts it — recording a structured
`last_run_ok`/`last_halt_reason` (not string-sniffing).

### 3.2 L1 — orchestration (the Agent Boss) **[built]**
The Orchestrator seeds `system`/`goal` MemoryNodes, decomposes the goal (rigid
workflow **or** dynamic decomposer), builds a `Plan` DAG from the steps, and
dispatches concurrently — independent branches run in parallel
(`_wait_for_any`), dependent steps (`depends_on`) receive their predecessor's
artifact node id as `context_pointers` (HuggingGPT-style resource dependency).
Each dispatch is a strict-JSON `TaskPayload` on `q.tasks.<role>`; the worker's
`StateUpdate` goes to the Critic; validated/failed results return on
`q.orchestrator.results`. Everything is a real bus message even though Phase A
runs the agents as threads in one process — splitting them into containers later
is deployment, not code.

**M2M discipline:** agents communicate only in typed JSON (`TaskPayload` /
`StateUpdate`), never free text; malformed messages raise `SchemaError`. The
Critic is fail-closed (missing payload ⇒ cannot validate ⇒ fail) and validation
commands are `shlex`-quoted.

### 3.3 L2 — the Data Matrix + Knowledge Base **[built]**
Shared long-term memory is a hybrid store: a vector index (Qdrant, or in-memory)
**fused with a knowledge graph** — every `MemoryNode` carries typed `edges`, a
`trust_score` (SYSTEM 1.0 > USER 0.9 > AGENT 0.6), and a `verbatim_required`
flag. `navigate(need, hops, seed_ids, trace_id)` does KNN + edge traversal +
trust-ranked fusion, scoped by trace so a stale goal never surfaces as
high-trust context for the current one.

**Knowledge Base** (`ingest.py`): a hierarchical taxonomy/outline parses into
`kb_root → kb_category (part_of) → kb_topic (belongs_to)` nodes stored verbatim
at reference trust, so agents land on a topic semantically and traverse up to
its domain for context. Shipped seed: `knowledge/agent_project_kb.md` (12
domains, 62 topics). CLI: `python main.py --ingest FILE [--kb-query NEED]`.

**[designed] entity graph:** add a stdlib SQLite triple store
`(subject, predicate, object, trace, ts)` for *relationships* ("what did I
promise Sarah about the Q3 deck?"). Vector recalls text; the graph answers
relationships. This is the memory tier a secretary needs and is deliberately
dependency-light.

### 3.4 L3 — roles & the Tools API **[built]**
Each role gets its own registry: `build_registry_for_role(role,
forbidden_modules, access_level)` filters `build_default_registry()` down to the
role's allow-list, removes per-task forbidden tools, and — at `restricted`
access — strips every tool in `_MUTATING_TOOLS`. Unknown role/level/tool fails
loudly at startup (a typo is a wiring bug, not a silently under-tooled worker).

| Role | Tools (allow-list) | Egress / power |
|---|---|---|
| `backend_dev` | files, shell, calculate, think | full local |
| `critic` | read/list/shell, think (read-execute only) | verifies, never fixes |
| `researcher` | `web_fetch`, files (no shell) | network in, no OS |
| `navigator` | `ors_*`, calculate, files | geo API |
| `voice` | `speak`, `transcribe`, files | audio sidecars |
| **`assistant`** | `pa_*`, `web_fetch`, read/write, think | local secretary data, read-only web |
| **`operator`** | `open_url`/`open_path`, `clipboard_*`, `notify`, `screenshot`, read/list | desktop; **no shell, no file writes** |

**Access-level containment:** `operator` and `researcher` never get shell.
Outward `operator` tools (`open_url`, `open_path`, `clipboard_set`, `notify`)
are in `_MUTATING_TOOLS`, so a **`restricted` operator is perception-only**
(`screenshot` + `clipboard_get`) — the containment that neutralises screen-borne
prompt injection (§5).

### 3.5 Real-time, bidirectional, voice **[built]**
- **Downstream (system → user):** `EventHub` fans lifecycle events
  (`goal.started`, `task.dispatched`, `task.validated`, `task.failed`,
  `goal.completed`) to the SSE endpoint `/api/events`; the web control panel
  renders a live Task Ledger. Bounded lossy per-subscriber queues + history
  replay so a slow client never blocks the swarm.
- **Upstream (user → system):** the web panel `POST /api/goals`; the `--swarm`
  REPL; voice in via `transcribe`.
- **[designed] steering channel:** a `q.control.<trace>` queue the Monitor
  checks between iterations, so a user can inject a new instruction or `cancel`
  mid-task — real-time interruption without preempting a single tool call.
  Voice barge-in stops TTS on user speech.

### 3.6 Governance substrate **[built]**
- **Personas** (`personas/*.md`) give each role a mission + rules compiled into
  its system suffix (Forge, Vigil, Scout, Atlas, Echo, **Ada** the secretary,
  **Otto** the operator).
- **CostGovernor** — hourly + per-run token circuit breaker; the worker refuses
  before executing when tripped.
- **Guardrails** — protected-path deny-list + destructive-shell pattern
  blocking, wired into file/shell tools and reused by `open_path`/`screenshot`.

---

## 4. The JARVIS capability layers (merged)

### 4.1 Computer use — the `operator` role **[built seam]**
A thin, backend-pluggable interface over the desktop, following the canonical
Anthropic/Operator perception→action shape: the tools stay small and
deterministic; the model supplies the intelligence.

Tools (all return `"ERROR: ..."` on any missing backend, never raise; all
subprocess calls pass an **argv list, never `shell=True`**):
`open_url(url)` (http/https only, reuses `web_fetch` SSRF policy),
`open_path(path)` (OS opener, guardrail-checked), `clipboard_get()`,
`clipboard_set(text)`, `notify(title, message)`, `screenshot(output_path)`.
Backends degrade down a ladder (mss → grim/scrot/screencapture; pyperclip →
wl-copy/xclip/pbcopy; notify-send/osascript) so the same interface runs on a
full desktop and no-ops cleanly in a headless container.

**[designed] full perception→action loop:** add `computer_screenshot` →
`computer_click/move/type/key/scroll` with **downscaled screenshots** (≤1280px,
scale coords back) to control token cost; prefer an **accessibility-tree**
(`ax_tree`/`ax_click`) over pixel-clicking where the OS a11y API is available —
cheaper, more reliable, more testable (the direction Agent-S / Playwright MCP
converged on). Run the target desktop in a **sandbox** (container/VM/dedicated
user; Anthropic's Docker+Xvfb+xdotool reference), never the primary session.

### 4.2 Web use — `researcher` now, `browser` next
**[built]** `web_fetch` is a hardened read: http/https only, every hop's A/AAAA
resolved and checked against loopback/private/link-local/metadata ranges,
redirects re-validated per hop, 100 KB body cap, always returns a string.

**[designed] `browser` role** on Playwright (Chromium is pre-installed in the
runtime): a persistent, per-user browser context with **indexed-DOM / a11y-
snapshot perception** (`browser_read_page` returns numbered interactive elements,
not raw HTML or pixels — the browser-use / Playwright-MCP pattern), actions by
selector/index (`navigate`, `click`, `fill`, `select`, `press`, `scroll`),
downloads quarantined. The same SSRF guard runs *before* navigation; if
Playwright is absent it **degrades to `web_fetch`** (read-only, no JS), so web
capability never hard-fails.

### 4.3 Personal assistant / secretary — the `assistant` role **[built local tier]**
A dependency-light, offline-first secretary persisting JSON under
`MYTHOS_ASSISTANT_DIR` (default `~/.mythos/assistant`):

| Capability | Tools |
|---|---|
| Tasks | `pa_add_task(text, due, priority)`, `pa_list_tasks(status)`, `pa_complete_task(id)` |
| Notes | `pa_add_note(text, tags)`, `pa_list_notes(query)` |
| Reminders | `pa_set_reminder(text, at)`, `pa_due_reminders(now)` |
| E-mail | `pa_draft_email(to, subject, body)` — **draft only** |
| Briefing | `pa_daily_brief(date)` — composes open tasks + due reminders + recent notes |

**Drafting is safe; sending is not implemented** — sending an e-mail, creating a
calendar invite, or deleting an event are outward/irreversible actions that
belong behind the human-in-the-loop gate (§5). This local tier is the
`connectors/local.py` of the design:

**[designed] integration-adapter pattern.** Define `CalendarAdapter`,
`MailAdapter`, `TaskAdapter`, `ContactAdapter` ABCs; back them with `local`
(ICS/Maildir/JSON — the offline default), `google`, `microsoft`, or `caldav_imap`
implementations selected by config. The agent's tool surface stays identical
regardless of backend; contacts/preferences live in the Data Matrix as durable
`TRUST_USER` nodes so the secretary personalises over time. (MCP connectors for
Google Calendar/Gmail/Todoist become one adapter each behind these ABCs.)

### 4.4 Singularity orchestration — Boss + Scheduler **[designed]**
- **Boss front-end** (§2): fast-model conversational controller; reactive-now vs
  delegate; streams progress from `EventHub`/ledger; renders HITL previews.
- **Scheduler daemon:** ledger-backed declarative routines
  `{trigger, goal, access_level, notify}` — time-triggered (cron-like) and
  event-triggered (new VIP e-mail → triage + notify). Fires goals onto the bus,
  runs watchdogs (retry stuck traces), enforces quiet hours and rate limits.
  Survives restart via the persisted routine table. This is the line between a
  chatbot and an assistant.

---

## 5. Safety & governance (computer + web + personal data)

The load-bearing section: the system now has shell-adjacent power, untrusted
screen/page input, and the user's private data. Extends [SECURITY.md](SECURITY.md).

1. **Least privilege by construction [built].** Every new role defaults to the
   lowest level that works. `operator`/`researcher` never get shell; `assistant`
   gets domain tools, not OS tools. `restricted` strips mutating/outward tools.
2. **Human-in-the-loop for outward/irreversible actions [designed →
   `approvals.py`].** Classify each call `safe | reversible | outward |
   destructive`; **outward** (send, post, purchase, invite) and **destructive**
   (delete, overwrite, install) pause for a human who approves the *effect* via
   a **preview** (the e-mail body, the file diff, the calendar change) — modelled
   on Operator's `pending_safety_checks`. Allow/deny lists by action class +
   target. (Today's building blocks: drafting-not-sending in `assistant`;
   perception-only `restricted` operator; guardrail deny-lists.)
3. **Prompt-injection defense — screen & page are untrusted input.** Primary
   control is **privilege separation [built]**: the roles that ingest untrusted
   pixels/DOM (`operator`, `browser`) cannot execute privileged actions, so
   injection can at most *propose* an action the gate catches. Reinforced by
   **[designed]** provenance tagging (untrusted content stored/passed as
   low-trust, "data not instructions"), **two-hop containment** (a task that
   consumed untrusted content becomes `restricted` downstream), and the outward-
   action gate as backstop.
4. **Audit logging [designed].** Every `computer_*`/`browser_*`/`mail_send`/
   `run_shell` appends `{trace, role, tool, args_hash, action_class, approval,
   result, tokens, ts}` to the append-only ledger; screenshots are summarised,
   not stored raw. Gives a full "what did my assistant do today" replay.
5. **Cost governance [built → extend].** Reuse `CostGovernor`; add per-capability
   sub-budgets (vision/browser reads are token-heavy), per-routine budgets, and
   a real-money ceiling with a daily spend digest.
6. **Credential broker [designed].** Secrets live outside the model's context; a
   tool requests "use credential X for host Y", the broker injects it at the
   HTTP/browser layer and returns only success — so injection can't exfiltrate a
   secret.
7. **Infra hardening [roadmap, prerequisite].** Authenticate Qdrant/RabbitMQ and
   bind to loopback before granting any agent computer/browser reach on a
   networked host (closes SECURITY gaps T1 injection→shell and T4 matrix
   poisoning).

---

## 6. Worked data flows

**A. "Plan my drive to Eilat and read it to me."** `--dynamic` → decomposer
prefilters `navigator` + `voice` → strict-JSON steps `[navigator → voice
(depends_on 0)]` → Orchestrator dispatches `navigator` (`ors_directions` →
route file, artifact upserted to the Matrix) → its node id flows as
`context_pointers` to `voice` → `voice` reads it, `speak` → audio → Critic
validates each → `goal.completed` streams over SSE. **[built]**

**B. "Every morning at 7, brief me."** Scheduler routine `{cron: 0 7 * * *,
goal: "daily briefing", role: assistant}` fires a goal → `assistant` runs
`pa_daily_brief` (open tasks + due reminders + recent notes) → Boss delivers it
(text + optional `speak`) → any follow-up outward action (e.g. "reply to Sarah")
is drafted, then gated for approval. **[designed] scheduler/Boss over [built]
assistant tools.**

**C. "Open the Q3 deck and copy the revenue table."** Boss routes to `operator`
→ `open_path(deck)` (guardrail-checked) → `screenshot` (perception) → model
reads the table → `clipboard_set(...)`. Because `operator` has no shell and the
screen is untrusted, any instruction embedded in the document cannot escalate;
an outward step (e.g. "email it to finance") would hit the approval gate.
**[built] operator seam; [designed] approval gate.**

---

## 7. Configuration & extension surface

- **Add a role:** append its allow-list to `ROLE_TOOLS`, drop a `<role>.md`
  persona, add decomposer keywords/description — the runtime auto-starts a
  worker for it in dynamic mode. (That is exactly how `assistant`/`operator`
  were added.)
- **Add a capability:** write a `tools_<x>.py` exporting `X_TOOLS`, register it
  in `build_default_registry`, reference the names in the relevant role.
- **Key env vars:** `MYTHOS_ASSISTANT_DIR`, `MYTHOS_TTS_URL`/`MYTHOS_ASR_URL`,
  `ORS_API_KEY`/`MYTHOS_ORS_URL`, `MYTHOS_BUS`/`MYTHOS_MATRIX`,
  `MYTHOS_HOURLY_TOKEN_BUDGET`/`MYTHOS_RUN_TOKEN_BUDGET`, `MYTHOS_GUARDRAILS`,
  `MYTHOS_PERSONA_DIR`. Full tables in [OPERATIONS.md](OPERATIONS.md).

---

## 8. Build order (A → Z, lowest risk → highest capability)

1. **[built]** Core agent, swarm, Data Matrix, Critic loop, dynamic decomposer.
2. **[built]** Real token governance, personas, cost governor, ledger, guardrails.
3. **[built]** Real-time SSE + web panel, voice I/O, PC edition (doctor/launchers).
4. **[built]** Knowledge Base ingestion.
5. **[built]** `assistant` (secretary, local tier) + `operator` (computer-use seam).
6. **[roadmap]** Infra hardening (auth Qdrant/RabbitMQ, loopback) — prerequisite.
7. **[designed]** `approvals.py` HITL gate + audit ledger schema — before any send/delete.
8. **[designed]** Connector adapters (calendar/mail/tasks) behind the ABCs.
9. **[designed]** `browser` role (Playwright, indexed-DOM) with `web_fetch` fallback.
10. **[designed]** Scheduler daemon (routines/briefings) + steering channel.
11. **[designed]** Boss conversational front-end (always-on, streaming, voice barge-in).
12. **[roadmap]** `operator` full perception→action loop in a sandboxed desktop.

---

## 9. Status matrix

| Capability | State | Where |
|---|---|---|
| R→A→O single agent | **built** | `mythos/agent.py` |
| Multi-agent swarm + Critic loop | **built** | `orchestration/` |
| Data Matrix (vector+graph) | **built** | `matrix.py` |
| Knowledge Base ingestion | **built** | `ingest.py`, `knowledge/` |
| Real-time SSE + control panel | **built** | `events.py`, `server.py` |
| Voice in/out | **built** | `tools_asr.py`, `tools_tts.py` |
| Cost governor / guardrails / personas | **built** | `governor.py`, `guardrails.py`, `personas/` |
| **Digital secretary (local)** | **built** | `tools_assistant.py`, `assistant` role |
| **Computer-use seam** | **built** | `tools_computer.py`, `operator` role |
| Web fetch (SSRF-hardened) | **built** | `tools_web.py`, `researcher` |
| **Local / free-model provider** | **built** | `llm.py` `LocalLLM` (Ollama/OpenAI-compatible) |
| **Specialist persona library** | **built** | `personas/library/` (24 imported specialists) + `--persona` |
| Entity/relationship graph memory | designed | §3.3 |
| HITL approvals + audit | designed | §5 |
| Connector adapters (cloud cal/mail) | designed | §4.3 |
| Browser automation role | designed | §4.2 |
| Scheduler + steering + Boss front-end | designed | §2, §4.4 |
| Full computer perception→action loop | roadmap | §4.1 |

---

*Together and apart (ביחד ולחוד): each layer is independently useful and
testable, and they compose into one responsive, governed, always-available
assistant. This blueprint is the map; the **[built]** rows are the territory
already walked.*
