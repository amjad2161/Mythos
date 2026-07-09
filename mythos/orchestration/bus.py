"""
mythos/orchestration/bus.py
---------------------------
Asynchronous message transport between agents.

All inter-agent traffic flows through named queues carrying JSON envelope
strings (see ``schemas.py``).  Two drivers implement the same interface:

* ``RabbitMQBus``  – production transport over AMQP (pika).  One dedicated
  connection per consuming thread (pika connections are not thread-safe);
  publishers get a thread-local connection.
* ``InMemoryBus``  – ``queue.Queue`` per queue name, for offline demos and
  unit tests.  Same at-least-once semantics: a handler crash requeues the
  message once, then drops it.

Queue topology for the Phase A rigid workflow:

    q.tasks.<role>            orchestrator/critic -> worker
    q.critic.review           worker -> critic (every result is intercepted)
    q.orchestrator.results    critic -> orchestrator (validated/terminal only)
"""
from __future__ import annotations

import queue
import threading
from abc import ABC, abstractmethod
from typing import Callable, Dict, Tuple

TASK_QUEUE_PREFIX = "q.tasks."
CRITIC_QUEUE = "q.critic.review"
RESULTS_QUEUE = "q.orchestrator.results"


def task_queue(role: str) -> str:
    """Queue a worker of *role* consumes its TaskPayloads from."""
    return f"{TASK_QUEUE_PREFIX}{role}"


class MessageBus(ABC):
    """Abstract message transport."""

    @abstractmethod
    def declare_queue(self, name: str) -> None:
        """Ensure *name* exists (idempotent)."""

    @abstractmethod
    def publish(self, queue_name: str, body: str) -> None:
        """Enqueue *body* (a JSON envelope string) onto *queue_name*."""

    @abstractmethod
    def consume(
        self,
        queue_name: str,
        handler: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        """
        Block, delivering each message body to *handler*, until *stop_event*
        is set.  A message whose handler raises is redelivered once and then
        dropped (at-least-once, bounded).  Intended to run in its own thread.
        """

    @abstractmethod
    def close(self) -> None:
        """Release transport resources."""


# ---------------------------------------------------------------------------
# In-memory driver (offline demos / unit tests)
# ---------------------------------------------------------------------------

class InMemoryBus(MessageBus):
    """Process-local bus: one ``queue.Queue`` per queue name."""

    _POLL_S = 0.05

    def __init__(self) -> None:
        self._queues: Dict[str, "queue.Queue[Tuple[str, bool]]"] = {}
        self._lock = threading.Lock()

    def _get(self, name: str) -> "queue.Queue[Tuple[str, bool]]":
        with self._lock:
            if name not in self._queues:
                self._queues[name] = queue.Queue()
            return self._queues[name]

    def declare_queue(self, name: str) -> None:
        self._get(name)

    def publish(self, queue_name: str, body: str) -> None:
        self._get(queue_name).put((body, False))

    def consume(
        self,
        queue_name: str,
        handler: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        q = self._get(queue_name)
        while not stop_event.is_set():
            try:
                body, redelivered = q.get(timeout=self._POLL_S)
            except queue.Empty:
                continue
            try:
                handler(body)
            except Exception as exc:  # noqa: BLE001 – bus must survive handler bugs
                if not redelivered:
                    q.put((body, True))
                else:
                    print(f"[bus] message on '{queue_name}' dropped after redelivery: {exc}")

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# RabbitMQ driver (production)
# ---------------------------------------------------------------------------

class RabbitMQBus(MessageBus):
    """
    AMQP transport backed by RabbitMQ.

    Durable queues on the default exchange (routing key == queue name) keep
    the topology declarative and 1:1 with agent roles.  Manual acks: a message
    is acked only after its handler returns; a raising handler nacks with
    requeue on first delivery and drops on redelivery.
    """

    def __init__(self, broker_url: str) -> None:
        try:
            import pika  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "The 'pika' package is required for the RabbitMQ bus. "
                "Install it with: pip install mythos[orchestration]"
            ) from exc
        self._pika = pika
        self._params = pika.URLParameters(broker_url)
        self._local = threading.local()

    # -- connection management ------------------------------------------

    def _channel(self):  # noqa: ANN202 – pika channel, per-thread
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.is_closed:
            conn = self._pika.BlockingConnection(self._params)
            self._local.conn = conn
            self._local.chan = conn.channel()
        chan = self._local.chan
        if chan.is_closed:
            self._local.chan = chan = conn.channel()
        return chan

    # -- MessageBus interface -------------------------------------------

    def declare_queue(self, name: str) -> None:
        self._channel().queue_declare(queue=name, durable=True)

    def publish(self, queue_name: str, body: str) -> None:
        self._channel().basic_publish(
            exchange="",
            routing_key=queue_name,
            body=body.encode("utf-8"),
            properties=self._pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,  # persistent
            ),
        )

    def consume(
        self,
        queue_name: str,
        handler: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        # A dedicated connection for this consuming thread.
        conn = self._pika.BlockingConnection(self._params)
        chan = conn.channel()
        chan.queue_declare(queue=queue_name, durable=True)
        chan.basic_qos(prefetch_count=1)
        try:
            # inactivity_timeout lets the loop poll stop_event between messages.
            for method, _props, body in chan.consume(queue_name, inactivity_timeout=0.2):
                if stop_event.is_set():
                    break
                if method is None:
                    continue
                try:
                    handler(body.decode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    requeue = not method.redelivered
                    chan.basic_nack(delivery_tag=method.delivery_tag, requeue=requeue)
                    if not requeue:
                        print(f"[bus] message on '{queue_name}' dropped after redelivery: {exc}")
                else:
                    chan.basic_ack(delivery_tag=method.delivery_tag)
        finally:
            try:
                chan.cancel()
                conn.close()
            except Exception:  # noqa: BLE001, S110 – best-effort teardown
                pass

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None and conn.is_open:
            conn.close()
