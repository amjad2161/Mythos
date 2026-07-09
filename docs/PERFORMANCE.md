# Mythos — Performance & Capacity Report

Measured by the team's performance engineer with `scripts/bench.py` on the
delivery environment (Linux container, Python 3.13, in-memory drivers, stub
LLM). These numbers isolate the **framework overhead**: in production the LLM
calls dominate end-to-end latency by 3–5 orders of magnitude, so the goal of
this report is to establish that the orchestration layer itself never becomes
the bottleneck on a personal machine.

## Measured results (v0.2.0)

| Subsystem | Benchmark | Result |
|---|---|---|
| Message bus (InMemory) | 20,000 publish→consume round trips | **~226,000 msgs/sec** (0.088 s total) |
| Hash embedder | 5,000 embeddings of a ~150-char text | **~19,600 embeds/sec** |
| Data Matrix (InMemory) | 2,000 node upserts | **~19,200 upserts/sec** |
| Data Matrix (InMemory) | KNN search over 2,000 nodes | **p50 78 ms · p95 85 ms** |
| Full swarm loop | goal → dispatch → worker → critic → validated | **p50 4.5 ms · p95 23.8 ms** |

Reproduce with:

```bash
python scripts/bench.py
```

## Interpretation

1. **The framework adds ~5 ms per subtask.** A full goal round trip through
   the orchestrator, bus, worker, artifact upsert, critic validation, and
   ledger updates costs single-digit milliseconds. Against a typical
   30–120 s LLM subtask, framework overhead is < 0.02%.
2. **The bus is never the constraint.** At ~226k msgs/sec in-process (and
   RabbitMQ comfortably handling tens of thousands/sec on localhost), the
   swarm's actual message volume — a handful of envelopes per subtask — is
   noise. Latency budgets should be spent on LLM calls and validation
   commands, nowhere else.
3. **Brute-force matrix search is the first real scaling cliff.** The
   in-memory driver scans all nodes per query: ~80 ms at 2k nodes grows
   linearly (~0.8 s at 20k). The production Qdrant driver replaces this with
   an ANN index and stays in single-digit milliseconds at that scale — use
   `--matrix qdrant` (the default) for long-lived installs; the in-memory
   driver is for tests and demos.
4. **Concurrency scales across roles.** Independent DAG branches dispatch
   immediately; with one worker thread per role, maximum parallelism equals
   the number of distinct roles in the plan (5 today). Since workers spend
   their time blocked on LLM I/O, thread-level parallelism is effectively
   free; N-workers-per-role is the Phase C scale-out knob.
5. **Where wall-clock actually goes** (live provider, typical coding goal):
   LLM calls ≈ 95–99%; validation commands (pytest runs etc.) ≈ 1–5%;
   everything else < 0.1%. Prompt caching (enabled by default on the
   Anthropic backend) removes the repeated system-prompt cost from every
   loop iteration after the first.

## Capacity guidance for a PC install

| Dimension | Practical envelope (v0.2.0) |
|---|---|
| Concurrent goals | 1 (the panel queues extra submissions serially by design) |
| Subtasks per goal | ≤ `decomposer_max_steps` (6 default); rigid workflows unbounded but sequential-per-dependency |
| Parallel subtasks | ≤ number of distinct roles in the plan (5) |
| Matrix size | Tens of thousands of nodes on Qdrant; keep in-memory under ~5k |
| Token throughput | Bounded by provider rate limits + `MYTHOS_HOURLY_TOKEN_BUDGET`, not by Mythos |
| Memory footprint | Swarm process ~60–120 MB + Qdrant/RabbitMQ containers (~500 MB combined) |

## Known performance limitations

- The critic serializes validations (one consumer); with many parallel
  branches, validation becomes the convoy point before the orchestrator.
- The control panel executes runs serially over one shared runtime; it is a
  control panel, not a job farm.
- Cooperative deadlines mean a single blocked LLM/tool call is not
  preempted mid-flight (hard kills arrive with process-per-agent, Phase C).
