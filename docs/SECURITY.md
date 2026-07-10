# Mythos v0.2 — Threat Model & Permissions Review

**Status:** Reviewed against source at commit time (2026-07). Scope: single-user PC install (`mythos --serve` / `--swarm`) with Docker-hosted RabbitMQ + Qdrant.
**Owner:** Security Architecture. This document is normative for v0.2; gaps marked **GAP** have tracked severity and are either scheduled or explicitly accepted in §5.

---

## 1. System Overview, Trust Boundaries, and Assets

### 1.1 Trust boundary diagram

```
                                   TB0: machine boundary
  ┌────────────────────────────────────────────────────────────────────────────┐
  │                                                                            │
  │  User (browser) ──HTTP──▶ Control Panel  127.0.0.1:8642, NO AUTH           │
  │        │                  (orchestration/server.py)                        │
  │        ▼ POST /api/goals                                                   │
  │  ┌───────────────┐   TaskPayload    ┌──────────────────────────────┐       │
  │  │ Orchestrator  │ ───────────────▶ │ RabbitMQ  :5672 (container,  │◀─TB3──┼── LAN?
  │  │ (orchestrator │   q.tasks.<role> │  default creds mythos/mythos)│       │
  │  │  .py)         │ ◀─────────────── └──────────────────────────────┘       │
  │  └───────────────┘  q.orchestrator.results          │                      │
  │        ▲ VALIDATED/FAILURE only                     ▼                      │
  │  ┌───────────┐  q.critic.review  ┌────────────────────────────┐            │
  │  │  Critic   │ ◀──────────────── │ Workers (worker.py, one    │            │
  │  │(critic.py)│  every result     │ per role: backend_dev,     │            │
  │  └───────────┘                   │ researcher, navigator, …)  │            │
  │     │  read/exec-only tools      └────────────────────────────┘            │
  │     │                                 │ per-role Tools API (roles.py)      │
  │     ▼                                 ▼                                    │
  │  ┌──────────────────────────────────────────────────────────┐              │
  │  │ Tools layer (tools.py / tools_web.py / tools_geo / tts)  │              │
  │  │  run_shell ──▶ host shell (TB1: agent → OS, UNSANDBOXED) │              │
  │  │  read/write/append_file ──▶ user's ENTIRE filesystem     │              │
  │  │  web_fetch ──▶ public internet (TB2: untrusted content)  │              │
  │  └──────────────────────────────────────────────────────────┘              │
  │        │                                                                   │
  │        ▼ upsert/navigate                                                   │
  │  ┌──────────────────────────────┐      ┌───────────────────────────┐       │
  │  │ Data Matrix / Qdrant :6333   │◀─TB3─┤ TTS sidecar :8000 (voice) │       │
  │  │ (container, NO AUTH)         │      └───────────────────────────┘       │
  │  └──────────────────────────────┘                                          │
  └────────────────────────────────────────────────────────────────────────────┘
           │ HTTPS (TB4)
           ▼
     LLM provider (Anthropic/OpenAI) — trusted for availability/confidentiality
     of prompts, NOT trusted to always produce safe tool calls
```

Boundary summary:

| Boundary | Crossing | Trust change |
|---|---|---|
| TB0 | Anything → the user's machine | Everything inside runs as the user's UID, no sandbox |
| TB1 | LLM-planned tool call → `run_shell` / file tools (`mythos/tools.py`) | Model output becomes OS action |
| TB2 | `web_fetch` body → agent context → Data Matrix (`mythos/tools_web.py`) | Internet-attacker text enters prompts |
| TB3 | Host/LAN → RabbitMQ 5672, Qdrant 6333/6334, TTS 8000 (`docker-compose.yml` publishes on all interfaces) | Network peer becomes bus/memory writer |
| TB4 | Prompts/keys → LLM provider (`mythos/llm.py`) | Data leaves the machine |

### 1.2 Assets at risk

1. **LLM API keys** — `ANTHROPIC_API_KEY` / `MYTHOS_API_KEY` in `~/.mythos/env` and process env (`mythos/envfile.py`, `mythos/config.py`). Compromise ⇒ financial spend + provider-account abuse.
2. **The user's filesystem** — file tools and `run_shell` (`mythos/tools.py`) have **no path confinement**; the agent's reach equals the invoking user's.
3. **Token spend** — every dispatched subtask burns paid tokens; retry loops (critic → worker, `max_attempts`) multiply cost.
4. **Data Matrix ground truth** — `MemoryNode` records with `trust_score` metadata (`mythos/orchestration/schemas.py`) steer every future agent's context via trust-ranked fusion (`matrix.py::navigate`/`fuse_context`). Poisoning it corrupts all subsequent work.
5. **Host integrity/lateral position** — a compromised agent is a shell on the user's PC inside their LAN.

---

## 2. Threat Analysis (STRIDE-aligned)

Severity: **Critical / High / Medium / Low** for the v0.2 PC deployment.

| # | Threat (STRIDE) | Vector | Existing mitigation (file) | Status |
|---|---|---|---|---|
| T1 | Prompt injection via fetched web content reaching a shell-capable agent (Tampering/EoP) | Attacker page fetched by `web_fetch` instructs the model; content persists as an `artifact` node (`worker.py::_execute` step 4) and can resurface via `matrix.navigate` semantic search into a later `backend_dev` (shell-capable) task | **Partial:** role separation — `researcher` has `web_fetch` but deliberately **no** `run_shell` (`roles.py::ROLE_TOOLS`); fetched text is capped at 100 kB (`tools_web.py`); artifacts are stored at `TRUST_AGENT=0.6`, below system (1.0) and user (0.9) trust (`schemas.py`); trace-scoped navigation blocks cross-run leakage (`matrix.py::navigate`) | **GAP — High.** Within one trace, researcher-written artifacts and files (`researcher` has `write_file`) flow into `backend_dev` context with no provenance marking or content quarantine. Injection → shell is a two-hop path, not blocked. Mitigate via `access_level: restricted` on tasks consuming untrusted content (§3.3) and provenance tagging (v0.3). |
| T2 | Command injection through `validation_command` (Tampering) | User goal text interpolated into a shell command run by the critic with `shell=True` (`critic.py::_validate_mechanically` → `tools.py::run_shell_command`) | **Mitigated for user text:** `WorkflowStep.validation_command()` inserts the goal via `shlex.quote` (`workflows.py:55`), regression-tested in `tests/orchestration/test_orchestrator.py::test_validation_command_shell_quotes_goal` | OK for rigid workflows. **Residual — Medium:** in `--dynamic` mode, decomposer-generated steps are `literal=True` and the LLM authors the whole command; it executes verbatim under the critic. Accepted per §5 (the same LLM already holds `run_shell` as backend_dev). |
| T3 | SSRF via `web_fetch` (Information Disclosure) | Agent coaxed into fetching `169.254.169.254`, RFC 1918, localhost services (incl. Qdrant/RabbitMQ admin on this very host) | **Mitigated:** http/https only; every hop's A/AAAA records must be public; loopback/private/link-local/reserved/multicast/unspecified refused; metadata endpoints blocklisted; redirects followed manually (max 5) and re-validated per hop; body capped (`tools_web.py::_host_is_blocked`, `_validate_url`, `_tool_web_fetch`) | OK. **Residual — Low/Medium:** DNS-rebinding TOCTOU is documented in the module header (validation resolves separately from connection). Accepted per §5. |
| T4 | Poisoned Data Matrix nodes (Tampering) | High-`trust_score` node injected so `fuse_context` presents attacker text as top-ranked "ground truth" | **Partial:** code paths set trust honestly — workers write artifacts at `TRUST_AGENT` (`worker.py`), only the orchestrator writes `TRUST_SYSTEM`/`TRUST_USER` nodes (`orchestrator.py::run`); trace filtering limits cross-goal reach (`matrix.py`) | **GAP — High.** `trust_score` is plain caller-supplied metadata with **no write-side enforcement**: Qdrant listens unauthenticated on 6333 (`docker-compose.yml`), so any LAN peer — or any agent using `run_shell`/`web_fetch`-adjacent tooling — can upsert a `trust_score: 1.0`, un-trace-tagged node that every future run fuses first. Requires Qdrant API key + server-side trust clamping (§4, v0.3). |
| T5 | Malicious/malformed bus messages (Spoofing/Tampering/DoS) | Crafted JSON on `q.tasks.<role>` or `q.critic.review` | **Partial:** strict envelope parsing — unknown enum values / missing fields raise `SchemaError`, never half-valid objects (`schemas.py::from_json`); a `StateUpdate` without its `task_payload` **fails closed** at the critic ("an unverifiable result never validates", `critic.py::_validate`); handler crashes are bounded to one redelivery then drop (`bus.py`); unknown roles fail loudly (`roles.py::build_registry_for_role`) | Schema layer OK. **GAP — Critical (network-conditional).** There is no message authentication: anyone who can reach RabbitMQ with `mythos/mythos` (T7) can publish a well-formed `TaskPayload{role: backend_dev, objective: "<anything>"}` and obtain arbitrary shell as the user. Entirely gated on broker exposure/creds. |
| T6 | Secrets handling (Information Disclosure) | Key theft from disk, logs, or bus traffic | **Partial:** keys live only in env/`~/.mythos/env`; env files never override explicit exports (`envfile.py`); keys are passed straight to SDK clients (`llm.py`) and are **never** embedded in prompts, `TaskPayload`s, `StateUpdate`s, or the Data Matrix; `--doctor` reports presence only, never the value (`doctor.py::_check_api_key`); dashboard/API expose no config values (`server.py::status`) | **GAP — Medium.** `write_env_template` (`envfile.py`) creates `~/.mythos/env` with default umask permissions — not `0600` — and `run_shell` agents can trivially `cat` it or `env`. Fix: `os.chmod(path, 0o600)` at creation + checklist item §4.1. |
| T7 | Control-panel exposure (Spoofing/EoP) | Dashboard has **no authentication, no CSRF token, no Host-header validation** (`server.py::_Handler`) | **Mitigated by default binding:** `127.0.0.1:8642` (`server.py::create_server`, `main.py --host` default); output is HTML-escaped client-side (`esc()` in `DASHBOARD_HTML`) | Acceptable **only** while loopback-bound. **GAP — Critical if `--host 0.0.0.0`:** `POST /api/goals` then equals "run arbitrary goals with the owner's API key and shell" for anyone on the network. Even loopback-bound, absent Host validation leaves a theoretical DNS-rebinding path to `/api/goals` (Medium). Never expose without a reverse proxy + auth (§4.3). |
| T8 | Docker services with default credentials (Spoofing/Tampering) | `RABBITMQ_DEFAULT_USER/PASS: mythos/mythos`; Qdrant no auth; all ports published without an interface prefix ⇒ bound on `0.0.0.0` (`docker-compose.yml`) | None in code | **GAP — High.** On a laptop on untrusted Wi-Fi this hands T4 + T5 to the LAN. Fix: bind `127.0.0.1:5672:5672` etc., change broker creds, set `MYTHOS_BROKER_URL` accordingly (§4.2). |
| T9 | Dependency / supply chain (Tampering) | Compromised PyPI packages (`pika`, `qdrant-client`, `fastembed`, `anthropic`, `openai`) | **Partial:** core runtime is stdlib-only (`tools.py`, `tools_web.py`, `envfile.py`, `server.py`); heavy deps are optional extras imported lazily; embedder runs locally (no key) | **GAP — Medium.** No version pinning/lockfile or hash checking; worse, the `supertonic` TTS container `pip install`s **unpinned at container start** (`docker-compose.yml` voice profile) — a fresh supply-chain roll of the dice on every recreate. Pin versions; pre-build the voice image. |
| T10 | Runaway token spend (DoS on wallet) | Retry loops, dynamic decomposition explosion, hostile "expensive" goals | **Mitigated:** `CostGovernor` — sliding hourly window + per-run budget, checked by every worker **before** accepting work, tripping to a structured `FAILURE` (`governor.py`, `worker.py::handle`); per-task hard token budget (`Constraints.max_compute_tokens`) enforced by the Monitor; independent iteration cap; bounded critic retries (`max_attempts`, `critic.py::_retry_or_escalate`); orchestrator waits under an absolute deadline (`orchestrator.py::_wait_for_any`); live spend on the dashboard (`server.py::status` → `tokens_last_hour`) | OK, **iff budgets are configured** — both default to 0 = unlimited (`envfile.py` template ships them commented out). Checklist §4.5. |
| T11 | Repudiation / auditability | Reconstructing what the swarm did | **Partial:** durable `TaskLedger` per goal (`ledger.py`, surfaced via `/api/runs/<id>`); verbatim `error_log` propagation; artifacts persisted with source + timestamp metadata | Adequate for v0.2; no tamper-evident log. Low. |
| T12 | Tool-output resource exhaustion (DoS) | Huge files/commands/expressions blowing the context or CPU | **Mitigated:** all tool output capped at 20 kB (`tools.py::_truncate`), reads capped, `run_shell` timeouts, calculator AST whitelist with size/complexity/exponent limits (`tools.py::_eval_node` guards) | OK. |

---

## 3. Permissions Model

### 3.1 As built

**Per-role tool allow-lists** (`mythos/orchestration/roles.py::ROLE_TOOLS`) — a worker's registry is the default registry filtered to its role, and an unknown role or a typo'd tool name **fails at startup**, never falling back to the full toolset:

| Role | Tools | Notes |
|---|---|---|
| `backend_dev` | read/write/append_file, list_directory, **run_shell**, calculate, current_time, think, finish | The only worker role with shell |
| `critic` | read_file, list_directory, **run_shell**, current_time, think, finish | **Read/execute-only by design** — no write tools; it verifies, it never fixes (asymmetry documented in `roles.py`; judgment prompt reinforces it, `critic.py::_JUDGMENT_PROMPT`) |
| `researcher` | **web_fetch**, read/write_file, list_directory, current_time, think, finish | Deliberately **no shell** — network and shell never co-reside in one role |
| `navigator` | ors_* geo tools, calculate, read/write_file, current_time, think, finish | Egress only to openrouteservice |
| `voice` | speak, read/write_file, list_directory, current_time, think, finish | TTS sidecar |

**Per-task subtraction:** `Constraints.forbidden_modules` (`schemas.py`) strips named tools from the role registry for one task (`roles.py::build_registry_for_role`); `finish` is un-bannable so the inner loop can always terminate.

**Fail-closed review:** the queue topology (`bus.py`) forces every worker result through the critic; missing work order or absent verdict ⇒ FAIL (`critic.py::_validate`, `_validate_by_judgment`).

### 3.2 Known enforcement gap

`TargetAgent.access_level` (`schemas.py:63`) is carried on every payload but **read by nothing** — a schema field with no teeth. §3.3 is the binding design closing this in v0.2.

### 3.3 Design: enforcing `TargetAgent.access_level` (normative)

Exactly three levels; unknown values must raise (fail loudly, mirroring unknown roles):

| Level | Semantics |
|---|---|
| `"restricted"` | Role registry **minus the write/execute set** `{"run_shell", "write_file", "append_file", "speak"}`. Read/reason-only variant of the role — the default choice for tasks whose inputs include untrusted content (T1). |
| `"standard"` | Role default (the `ROLE_TOOLS` allow-list). This is the payload default (`TargetAgent.access_level = "standard"`). |
| `"elevated"` | **Reserved.** Identical to `standard` in v0.2; exists so payloads can be authored forward-compatibly (future: broker-side approval, extra budget). Must not grant anything today. |

Enforcement point — `roles.build_registry_for_role`:

```python
WRITE_EXECUTE_TOOLS = {"run_shell", "write_file", "append_file", "speak"}
ACCESS_LEVELS = ("restricted", "standard", "elevated")

def build_registry_for_role(role, forbidden_modules=(), access_level="standard"):
    if access_level not in ACCESS_LEVELS:
        raise ValueError(f"Unknown access_level: {access_level!r}")
    banned = set(forbidden_modules)
    if access_level == "restricted":
        banned |= WRITE_EXECUTE_TOOLS
    # existing behaviour: "elevated" adds nothing; "finish" stays un-bannable;
    # filtering/validation logic unchanged.
```

Call-site wiring — the worker passes the payload's addressing block through (`worker.py::_execute`, step 3):

```python
registry = build_registry_for_role(
    self.role,
    constraints.forbidden_modules,
    access_level=payload.target_agent.access_level,
)
```

Rules: the level composes with `forbidden_modules` as a union of bans (both are subtractive; nothing can ever *add* a tool beyond the role list). The critic's own judgment registry stays `standard` for role `critic` (already write-free). Tests must cover: restricted strips exactly the four tools when the role has them, restricted on a role lacking them is a no-op, unknown level raises, elevated == standard.

---

## 4. Hardening Checklist — PC Install

1. **Secrets file:** `chmod 600 ~/.mythos/env`; keep keys out of `./.env` in shared repos (`envfile.py` loads both). Prefer exported env vars on multi-user machines (exports always win).
2. **Broker & matrix:** change `RABBITMQ_DEFAULT_USER/PASS` from `mythos/mythos` and set `MYTHOS_BROKER_URL` to match; add a Qdrant API key; prefix all published ports with `127.0.0.1:` in `docker-compose.yml` (currently they bind all interfaces).
3. **Control panel:** never run `--serve` with `--host 0.0.0.0` (`main.py`). The panel has no auth — remote access only via SSH tunnel (`ssh -L 8642:127.0.0.1:8642 host`) or an authenticating reverse proxy.
4. **Shell blast radius:** `run_shell` runs as *you* with no path confinement (`tools.py::_tool_run_shell` explicitly documents this). Run Mythos as a dedicated low-privilege user, or in a container/VM, when giving it goals that touch untrusted input; use `access_level: "restricted"` and `forbidden_modules: ["run_shell"]` where a task doesn't need execution.
5. **Budgets as blast-radius control:** set `MYTHOS_HOURLY_TOKEN_BUDGET` and `MYTHOS_RUN_TOKEN_BUDGET` (both default to unlimited) so a hijacked or looping run is financially bounded; watch `tokens/h` on the dashboard.
6. **Supply chain:** install pinned versions (`pip install mythos[orchestration]==<ver>` with a hash-checked lockfile); build the voice sidecar into a pinned image instead of the runtime `pip install supertonic`.
7. **After exposure changes, re-run** `mythos --doctor` and re-read this document's T5/T7/T8 rows — they are network-conditional.

---

## 5. Residual Risks Accepted for v0.2

| Risk | Rationale for acceptance |
|---|---|
| DNS-rebinding TOCTOU in `web_fetch` (T3) | Requires an attacker-controlled authoritative DNS server with sub-second TTLs racing the resolve→connect gap; the fix (pinning the validated IP through the TLS layer) is disproportionate for a stdlib-only client. Documented in `tools_web.py`. Internal services should additionally not trust localhost callers (see §4.2). |
| LLM-authored `validation_command` in `--dynamic` mode (T2 residual) | The command's author (the LLM) already holds unrestricted `run_shell` as `backend_dev`; sanitizing one channel while the other is open adds no real containment. Real containment is OS-level (§4.4) — v0.3 targets an opt-in sandbox. |
| Unsandboxed `run_shell` / unconfined file tools (T1/T12 substrate) | Full host reach **is the product** for a personal autonomous agent in v0.2. Compensating controls: role allow-lists, `restricted` access level, forbidden_modules, critic gate, budgets, and the operator checklist. Sandboxing (container/user-namespace execution) is the headline v0.3 item. |
| No message authentication on the bus (T5 residual) | Single-user, single-host deployment; the boundary control is broker credentials + loopback binding (§4.2). Signed envelopes are deferred until multi-host operation is supported. |
| No auth on the control panel (T7 residual) | Loopback-only by default and documented as "a local control panel, not a public service" (`server.py`); tunnel/proxy for remote use. A shared-secret header is queued for v0.3. |
| In-context prompt injection cannot be fully eliminated (T1 residual) | Trust-ranked fusion, role separation, and `restricted` tasks reduce—but cannot zero—the risk that persuasive fetched text steers a tool-bearing model. Treat any goal that ingests untrusted web content as running with that content's intent; budgets and the critic bound the damage. |

**Review cadence:** revisit at every role addition, every new tool with write/execute/network capability, and before any feature that binds a service beyond loopback.
