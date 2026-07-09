"""
tests/integration/conftest.py
-----------------------------
Fixtures for tests against live RabbitMQ + Qdrant (see docker-compose.yml).

Every fixture probes its service first and skips the test when the service
is unreachable, so a plain local `pytest` run never breaks.  In CI the
services are provided as GitHub Actions service containers.
"""
import os
import socket
import urllib.parse
import uuid

import pytest

BROKER_URL = os.getenv("MYTHOS_BROKER_URL", "amqp://mythos:mythos@localhost:5672/")
QDRANT_URL = os.getenv("MYTHOS_QDRANT_URL", "http://localhost:6333")


def _reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _url_host_port(url: str, default_port: int):
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname or "localhost", parsed.port or default_port


@pytest.fixture(scope="session")
def broker_url() -> str:
    pytest.importorskip("pika")
    host, port = _url_host_port(BROKER_URL, 5672)
    if not _reachable(host, port):
        pytest.skip(f"RabbitMQ unreachable at {host}:{port}")
    return BROKER_URL


@pytest.fixture(scope="session")
def qdrant_url() -> str:
    pytest.importorskip("qdrant_client")
    host, port = _url_host_port(QDRANT_URL, 6333)
    if not _reachable(host, port):
        pytest.skip(f"Qdrant unreachable at {host}:{port}")
    return QDRANT_URL


@pytest.fixture()
def unique_name() -> str:
    """A per-test unique suffix for queues/collections so runs never collide."""
    return uuid.uuid4().hex[:10]
