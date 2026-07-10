"""
mythos/ordering.py
------------------
Named, thread-safe ordering primitives — one honest home for the FIFO / LIFO
disciplines the system relies on, so the intent is explicit at every call site
instead of implicit in how a ``deque`` or ``list`` happens to be used.

* :class:`BoundedFifo` — a **queue**: first-in-first-out.  On overflow the
  *oldest* item is dropped (a lossy sliding window that keeps the most recent
  ``maxlen``).  This is the discipline behind event replay buffers, the token
  window, and the short-term memory window.
* :class:`BoundedLifo` — a **stack**: last-in-first-out.  On overflow the
  *oldest* item (the bottom of the stack) is dropped.  This is the discipline
  behind "most-recent-first" views like the activity log.

Both wrap ``collections.deque(maxlen=...)`` and guard every mutation with a lock
so they are safe to share across the swarm's threads.  ``maxlen=0`` means
unbounded.  See ``docs/ORDERING.md`` for the full end-to-end map of where each
discipline is used.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Generic, List, Optional, TypeVar

T = TypeVar("T")


class _Bounded(Generic[T]):
    """Shared machinery: a lock-guarded, optionally-bounded deque."""

    def __init__(self, maxlen: int = 0) -> None:
        if maxlen < 0:
            raise ValueError("maxlen must be >= 0 (0 = unbounded)")
        self._maxlen = maxlen or None
        self._items: Deque[T] = deque(maxlen=self._maxlen)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def maxlen(self) -> int:
        return self._maxlen or 0

    @property
    def full(self) -> bool:
        with self._lock:
            return self._maxlen is not None and len(self._items) >= self._maxlen

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def snapshot(self) -> List[T]:
        """A copy of the contents, oldest → newest."""
        with self._lock:
            return list(self._items)


class BoundedFifo(_Bounded[T]):
    """A first-in-first-out queue; overflow drops the oldest item."""

    def push(self, item: T) -> Optional[T]:
        """Enqueue *item*; return the evicted oldest item if this overflowed."""
        with self._lock:
            dropped = (
                self._items[0]
                if self._maxlen is not None and len(self._items) >= self._maxlen
                else None
            )
            self._items.append(item)         # newest at the right
            return dropped

    def pop(self) -> T:
        """Dequeue the oldest item (FIFO). Raises IndexError when empty."""
        with self._lock:
            return self._items.popleft()     # oldest at the left

    def peek(self) -> Optional[T]:
        """The oldest item without removing it (None when empty)."""
        with self._lock:
            return self._items[0] if self._items else None

    def recent(self, n: int) -> List[T]:
        """The *n* most-recent items, oldest → newest."""
        with self._lock:
            return list(self._items)[-n:] if n > 0 else []


class BoundedLifo(_Bounded[T]):
    """A last-in-first-out stack; overflow drops the oldest (bottom) item."""

    def push(self, item: T) -> Optional[T]:
        """Push *item* on top; return the evicted bottom item if this overflowed."""
        with self._lock:
            dropped = (
                self._items[0]
                if self._maxlen is not None and len(self._items) >= self._maxlen
                else None
            )
            self._items.append(item)         # top at the right
            return dropped

    def pop(self) -> T:
        """Pop the newest item (LIFO). Raises IndexError when empty."""
        with self._lock:
            return self._items.pop()         # top at the right

    def peek(self) -> Optional[T]:
        """The newest (top) item without removing it (None when empty)."""
        with self._lock:
            return self._items[-1] if self._items else None

    def newest_first(self) -> List[T]:
        """The contents top → bottom (most-recent first) — the display order."""
        with self._lock:
            return list(reversed(self._items))
