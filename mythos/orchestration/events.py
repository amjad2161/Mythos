"""
mythos/orchestration/events.py
------------------------------
In-process event stream for real-time observability.

The swarm is push-capable: the orchestrator, workers, and critic emit
lifecycle events (goal started, task dispatched, result validated, retry,
governor tripped, …) as they happen.  The control panel subscribes and
forwards them to the browser over Server-Sent Events, replacing polling with
immediate, bidirectional-feeling updates.

Design: a fan-out hub with one bounded ``queue.Queue`` per subscriber. Slow
or dead subscribers drop their oldest events rather than blocking producers
(the swarm must never stall because a browser tab is slow). Thread-safe;
stdlib only.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from ..ordering import BoundedFifo


@dataclass
class Event:
    """One observable moment in the swarm's lifecycle."""

    kind: str                       # e.g. "goal.started", "task.dispatched"
    seq: int = 0                    # monotonic per-hub sequence number
    ts_ms: int = 0                  # wall-clock millis (stamped by the hub)
    trace_id: str = ""
    task_id: str = ""
    role: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class _Subscription:
    """One consumer's bounded, lossy queue."""

    def __init__(self, maxsize: int) -> None:
        self._q: "queue.Queue[Optional[Event]]" = queue.Queue(maxsize=maxsize)

    def offer(self, event: Event) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            # Drop the oldest to make room – observability must never block
            # the swarm.
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(event)
            except queue.Full:
                pass

    def stream(self, stop: threading.Event, poll_s: float = 0.5) -> Iterator[Event]:
        while not stop.is_set():
            try:
                item = self._q.get(timeout=poll_s)
            except queue.Empty:
                yield _HEARTBEAT  # keep-alive so the HTTP connection stays open
                continue
            if item is None:
                return
            yield item

    def close(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass


# A sentinel heartbeat event the SSE layer renders as a comment line.
_HEARTBEAT = Event(kind="heartbeat")


class EventHub:
    """Thread-safe fan-out of :class:`Event`s to many subscribers."""

    def __init__(
        self,
        per_subscriber_buffer: int = 512,
        history: int = 200,
        audit: Any = None,
    ) -> None:
        self._subs: List[_Subscription] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._buffer = per_subscriber_buffer
        # Replay buffer: a FIFO sliding window that keeps the most recent
        # `history` events (drop-oldest on overflow) — see mythos.ordering.
        self._history: BoundedFifo = BoundedFifo(maxlen=history)
        # Optional durable sink: anything with append(kind, ts_ms=..., **detail)
        # (an AuditLog). Ephemeral fan-out stays the hub's job; persistence is
        # delegated so the two concerns don't entangle.
        self._audit = audit

    # -- producer side ---------------------------------------------------

    def emit(
        self,
        kind: str,
        *,
        trace_id: str = "",
        task_id: str = "",
        role: str = "",
        ts_ms: Optional[int] = None,
        **detail: Any,
    ) -> Event:
        """Publish an event to every subscriber (and the replay buffer)."""
        with self._lock:
            self._seq += 1
            stamp = ts_ms if ts_ms is not None else int(time.time() * 1000)
            event = Event(
                kind=kind,
                seq=self._seq,
                ts_ms=stamp,
                trace_id=trace_id,
                task_id=task_id,
                role=role,
                detail=detail,
            )
            self._history.push(event)   # FIFO drop-oldest at the bound
            subs = list(self._subs)
            audit = self._audit
        if audit is not None:
            # Persistence must never break event fan-out — the durable sink is
            # best-effort (a bad payload / disk error can't stall the swarm).
            try:
                audit.append(
                    kind, ts_ms=stamp, trace_id=trace_id, task_id=task_id, role=role, **detail
                )
            except Exception:  # noqa: BLE001 – audit is best-effort, never fatal
                pass
        for sub in subs:
            sub.offer(event)
        return event

    # -- consumer side ---------------------------------------------------

    def subscribe(self) -> _Subscription:
        sub = _Subscription(self._buffer)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: _Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
        sub.close()

    def recent(self, limit: int = 50) -> List[Event]:
        # Oldest → newest, most-recent `limit` (BoundedFifo is self-locked).
        return self._history.recent(limit)

    def close(self) -> None:
        with self._lock:
            subs = list(self._subs)
            self._subs.clear()
        for sub in subs:
            sub.close()


# A shared no-op hub so components can always call ``.emit`` unconditionally.
class _NullHub(EventHub):
    def emit(self, kind: str, **kwargs: Any) -> Event:  # noqa: D401
        return _HEARTBEAT

    def subscribe(self) -> _Subscription:
        return _Subscription(1)


NULL_HUB = _NullHub()
