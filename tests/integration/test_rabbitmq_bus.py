"""
tests/integration/test_rabbitmq_bus.py
--------------------------------------
RabbitMQBus against a live broker – the same contract InMemoryBus honours.
"""
import threading
import time

import pytest

from mythos.orchestration.bus import RabbitMQBus

pytestmark = pytest.mark.integration


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def test_publish_then_consume(broker_url, unique_name):
    bus = RabbitMQBus(broker_url)
    queue_name = f"q.test.{unique_name}"
    bus.declare_queue(queue_name)

    received = []
    stop = threading.Event()
    consumer = threading.Thread(
        target=bus.consume, args=(queue_name, received.append, stop), daemon=True
    )
    consumer.start()

    bus.publish(queue_name, '{"n": 1}')
    bus.publish(queue_name, '{"n": 2}')
    try:
        _wait_until(lambda: len(received) == 2)
    finally:
        stop.set()
        consumer.join(timeout=5)
        bus.close()

    assert received == ['{"n": 1}', '{"n": 2}']


def test_handler_crash_redelivers_once_then_drops(broker_url, unique_name):
    bus = RabbitMQBus(broker_url)
    queue_name = f"q.test.{unique_name}"
    bus.declare_queue(queue_name)

    attempts = []
    stop = threading.Event()

    def bad_handler(body):
        attempts.append(body)
        raise RuntimeError("handler bug")

    consumer = threading.Thread(
        target=bus.consume, args=(queue_name, bad_handler, stop), daemon=True
    )
    consumer.start()
    bus.publish(queue_name, "poison")
    try:
        _wait_until(lambda: len(attempts) == 2)
        time.sleep(0.5)  # prove no third delivery
    finally:
        stop.set()
        consumer.join(timeout=5)
        bus.close()

    assert attempts == ["poison", "poison"]


def test_cross_thread_publish_delivery(broker_url, unique_name):
    """Publishers on different threads reach the same consumer."""
    bus = RabbitMQBus(broker_url)
    queue_name = f"q.test.{unique_name}"
    bus.declare_queue(queue_name)

    received = []
    stop = threading.Event()
    consumer = threading.Thread(
        target=bus.consume, args=(queue_name, received.append, stop), daemon=True
    )
    consumer.start()

    publishers = [
        threading.Thread(target=bus.publish, args=(queue_name, f"msg-{i}"))
        for i in range(4)
    ]
    for t in publishers:
        t.start()
    for t in publishers:
        t.join(timeout=5)

    try:
        _wait_until(lambda: len(received) == 4)
    finally:
        stop.set()
        consumer.join(timeout=5)
        bus.close()

    assert sorted(received) == [f"msg-{i}" for i in range(4)]
