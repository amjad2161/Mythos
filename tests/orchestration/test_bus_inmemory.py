"""
tests/orchestration/test_bus_inmemory.py
----------------------------------------
Behavioural tests for the in-memory MessageBus driver (the same contract the
RabbitMQ driver honours – see tests/integration/test_rabbitmq_bus.py).
"""
import threading
import time

from mythos.orchestration.bus import InMemoryBus, task_queue


def collect(bus, queue_name, received, stop):
    bus.consume(queue_name, received.append, stop)


def test_task_queue_naming():
    assert task_queue("backend_dev") == "q.tasks.backend_dev"


def test_publish_then_consume():
    bus = InMemoryBus()
    received = []
    stop = threading.Event()
    thread = threading.Thread(target=collect, args=(bus, "q.test", received, stop))
    thread.start()

    bus.publish("q.test", '{"a": 1}')
    bus.publish("q.test", '{"a": 2}')
    _wait_until(lambda: len(received) == 2)
    stop.set()
    thread.join(timeout=2)

    assert received == ['{"a": 1}', '{"a": 2}']


def test_handler_crash_redelivers_once_then_drops():
    bus = InMemoryBus()
    attempts = []
    stop = threading.Event()

    def bad_handler(body):
        attempts.append(body)
        raise RuntimeError("handler bug")

    thread = threading.Thread(
        target=bus.consume, args=("q.test", bad_handler, stop)
    )
    thread.start()
    bus.publish("q.test", "poison")
    _wait_until(lambda: len(attempts) == 2)
    # Give the loop a moment to prove there is no third delivery.
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=2)

    assert attempts == ["poison", "poison"]


def test_queues_are_isolated():
    bus = InMemoryBus()
    a, b = [], []
    stop = threading.Event()
    ta = threading.Thread(target=collect, args=(bus, "q.a", a, stop))
    tb = threading.Thread(target=collect, args=(bus, "q.b", b, stop))
    ta.start()
    tb.start()

    bus.publish("q.a", "for-a")
    bus.publish("q.b", "for-b")
    _wait_until(lambda: a and b)
    stop.set()
    ta.join(timeout=2)
    tb.join(timeout=2)

    assert a == ["for-a"]
    assert b == ["for-b"]


def test_consume_stops_on_event():
    bus = InMemoryBus()
    stop = threading.Event()
    thread = threading.Thread(target=collect, args=(bus, "q.test", [], stop))
    thread.start()
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")
