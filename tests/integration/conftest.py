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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

BROKER_URL = os.getenv("MYTHOS_BROKER_URL", "amqp://mythos:mythos@localhost:5672/")
QDRANT_URL = os.getenv("MYTHOS_QDRANT_URL", "http://localhost:6333")
_READY_WAIT_S = 30.0


def _reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ready(url: str, wait_s: float = _READY_WAIT_S) -> bool:
    """Poll an HTTP endpoint until it answers – TCP accept alone does not
    guarantee the service is actually ready to serve requests."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except urllib.error.HTTPError:
            return True  # answered (even an error status means it's serving)
        except OSError:
            time.sleep(0.5)
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
    if not _http_ready(f"{QDRANT_URL}/readyz"):
        pytest.skip(f"Qdrant at {QDRANT_URL} accepted TCP but never became ready")
    return QDRANT_URL


@pytest.fixture()
def unique_name() -> str:
    """A per-test unique suffix for queues/collections so runs never collide."""
    return uuid.uuid4().hex[:10]
