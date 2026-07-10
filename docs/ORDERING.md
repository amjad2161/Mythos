# Ordering Map — FIFO / LIFO / Priority, end to end

Every place in Mythos that queues, buffers, windows, or evicts, and the exact
discipline it follows. The named primitives live in
[`mythos/ordering.py`](../mythos/ordering.py) (`BoundedFifo`, `BoundedLifo`);
the disciplines that are **priority or DAG** by design are called out so they
are never "corrected" into arrival-order queues.

Legend: **FIFO** first-in-first-out queue · **LIFO** last-in-first-out stack ·
**window** bounded, keep-newest (drop-oldest) · **priority** ordered by a key,
not arrival · **map** keyed, unordered.

## Transport & orchestration

| Where | Structure | Discipline | Notes |
|---|---|---|---|
| `bus.py` InMemoryBus `_queues[name]` | `queue.Queue` | **FIFO** | publish→tail, consume→head |
| `bus.py` RabbitMQBus queues | AMQP queue | **FIFO** | broker-ordered |
| `bus.py` requeue-on-failure (both drivers) | — | **FIFO, retry re-tailed** | a redelivered message loses its position (at-least-once, *not* order-preserving on retry). Documented, intended. |
| `orchestrator.py` `_results` | `queue.Queue` | **FIFO** | critic → orchestrator results |
| `orchestrator.py` `_unmatched` | `dict` | **map (last-wins)** | keyed by `task_id`, checked before blocking; a duplicate `task_id` overwrites — now logged, not silent |
| `orchestrator.py` `in_flight` / `constraints_in_flight` | `dict` | **map** | keyed pop by `task_id` |
| `orchestrator.py` `results_by_index` | `dict` | **index-ordered** | rendered `sorted(by step index)` |
| `server.py` RunManager `_queue` | `queue.Queue` | **FIFO** | serial run execution in submission order |
| `server.py` RunManager `_order` | `list` | **FIFO store, LIFO display** | append at tail; `list_runs` returns `reversed()` → newest-first to the UI |

## Buffers & windows (keep-newest, drop-oldest)

| Where | Structure | Discipline | Notes |
|---|---|---|---|
| `events.py` `EventHub._history` | **`BoundedFifo`** | **window** | replay buffer, cap 200 — refactored to the named primitive |
| `events.py` `_Subscription._q` | `queue.Queue(maxsize)` | **FIFO consume + window** | slow subscriber drops its *oldest* queued event (observability never blocks the swarm — lossy by design) |
| `monitor.py` `_events` | `deque(maxlen=200)` | **window** | agent event log |
| `monitor.py` `_recent_tool_calls` | `deque(maxlen)` | **window** | loop detection reads the whole window |
| `governor.py` `_events` | `deque` | **FIFO time-window** | 60-min sliding window, `popleft` while older than cutoff |
| `memory.py` `ShortTermMemory._messages` | `list` | **FIFO eviction** | drops the *oldest non-system* message (system prompt pinned; tool-result turns evicted as a pair) |
| `audit.py` `AuditLog._events` | `list` + JSONL | **FIFO append-only** | `seq = len`; `reduce_state` folds in insertion order — replay depends on stable FIFO |
| dashboard `#activity` (JS) | DOM list | **window, newest-first** | prepend newest, drop oldest at the bottom, cap 60 (display cap < server history 200, by design) |

## Priority / DAG — ordered by a key, NOT arrival (do not queue-ify)

| Where | Structure | Discipline | Notes |
|---|---|---|---|
| `matrix.py` `navigate` frontier | `list` | **BFS level-order** | breadth-first graph expansion per hop |
| `matrix.py` `navigate` return / `search` | sorted `list` | **priority** | ranked by trust score / cosine similarity, top-k |
| `planner.py` `Plan.next_task` | `list` scan | **DAG-ready, insertion-tiebreak** | first PENDING task whose deps are satisfied |
| `critic.py` `submit_verdict` capture | `list` | **LIFO (last-wins)** | if the model submits twice, the last verdict is honored (documented at the call site) |

## Deliberate caveats

1. **Retries are not order-preserving.** A nacked/redelivered bus message is
   re-queued at the tail. Mythos correlates results by `task_id`, not arrival
   order, so this is safe — but "strict end-to-end FIFO" does not hold across a
   retry, by design.
2. **Observability is lossy, never blocking.** `_Subscription` and the dashboard
   log both drop their oldest entries under pressure rather than stall the
   swarm. SSE clients may miss events; the durable record is the audit log.
3. **Priority orderings are load-bearing.** `matrix` (trust/similarity) and
   `planner` (DAG readiness) must stay priority-ordered — a blanket FIFO would
   break trust fusion and dependency scheduling.
4. **Display order is the inverse of storage order** for runs and the activity
   log (store FIFO, show newest-first). Keep the two straight when editing.
