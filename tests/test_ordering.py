"""
tests/test_ordering.py
----------------------
The named FIFO / LIFO ordering primitives.
"""
import threading

import pytest

from mythos.ordering import BoundedFifo, BoundedLifo


class TestBoundedFifo:
    def test_fifo_order(self):
        q = BoundedFifo()
        for i in range(3):
            q.push(i)
        assert q.pop() == 0        # oldest out first
        assert q.pop() == 1
        assert q.peek() == 2

    def test_overflow_drops_oldest(self):
        q = BoundedFifo(maxlen=3)
        assert q.push(1) is None
        q.push(2)
        q.push(3)
        dropped = q.push(4)        # overflow
        assert dropped == 1        # the oldest was evicted
        assert q.snapshot() == [2, 3, 4]
        assert len(q) == 3

    def test_recent_keeps_newest(self):
        q = BoundedFifo(maxlen=5)
        for i in range(5):
            q.push(i)
        assert q.recent(2) == [3, 4]

    def test_empty_pop_raises(self):
        with pytest.raises(IndexError):
            BoundedFifo().pop()

    def test_unbounded(self):
        q = BoundedFifo(maxlen=0)
        for i in range(1000):
            assert q.push(i) is None
        assert len(q) == 1000


class TestBoundedLifo:
    def test_lifo_order(self):
        s = BoundedLifo()
        for i in range(3):
            s.push(i)
        assert s.pop() == 2        # newest out first
        assert s.pop() == 1
        assert s.peek() == 0

    def test_overflow_drops_oldest_bottom(self):
        s = BoundedLifo(maxlen=3)
        for i in range(3):
            s.push(i)
        dropped = s.push(9)        # overflow drops the bottom (oldest)
        assert dropped == 0
        assert s.snapshot() == [1, 2, 9]      # oldest → newest
        assert s.newest_first() == [9, 2, 1]  # display order

    def test_empty_pop_raises(self):
        with pytest.raises(IndexError):
            BoundedLifo().pop()


class TestThreadSafety:
    def test_concurrent_pushes_bounded(self):
        q = BoundedFifo(maxlen=500)

        def worker():
            for i in range(1000):
                q.push(i)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # never exceeds the bound despite concurrent writers
        assert len(q) == 500
        assert q.maxlen == 500
