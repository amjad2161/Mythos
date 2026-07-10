"""
mythos/doctor.py
----------------
Environment diagnostics for PC installs (``mythos --doctor``).

Checks the pieces a local deployment needs and reports each as OK / WARN /
FAIL with a one-line hint.  WARNs cover optional capabilities (voice,
navigation, real infrastructure); FAILs cover things nothing works without.
"""
from __future__ import annotations

import importlib
import os
import socket
import sys
import urllib.parse
from dataclasses import dataclass
from typing import List

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    status: str        # OK | WARN | FAIL
    detail: str


def _reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_python() -> CheckResult:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 9):
        return CheckResult("Python", FAIL, f"{version} - Mythos needs Python 3.9+")
    return CheckResult("Python", OK, version)


def _check_api_key() -> CheckResult:
    if os.getenv("MYTHOS_API_KEY") or os.getenv("ANTHROPIC_API_KEY"):
        return CheckResult("LLM API key", OK, "found in the environment")
    return CheckResult(
        "LLM API key", FAIL,
        "set ANTHROPIC_API_KEY (in ~/.mythos/env or the environment); "
        "only --provider stub works without it",
    )


def _check_package(package: str, capability: str) -> CheckResult:
    try:
        importlib.import_module(package)
    except ImportError:
        return CheckResult(
            f"package: {package}", WARN,
            f"{capability} unavailable - pip install \"mythos[orchestration]\"",
        )
    return CheckResult(f"package: {package}", OK, capability)


def _check_service(name: str, url: str, default_port: int, hint: str) -> CheckResult:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or default_port
    if _reachable(host, port):
        return CheckResult(name, OK, f"reachable at {host}:{port}")
    return CheckResult(name, WARN, f"unreachable at {host}:{port} - {hint}")


def run_doctor() -> List[CheckResult]:
    """Run every check and return the results (no printing)."""
    results = [
        _check_python(),
        _check_api_key(),
        _check_package("anthropic", "Claude backend"),
        _check_package("pika", "RabbitMQ message bus"),
        _check_package("qdrant_client", "Qdrant Data Matrix"),
        _check_package("fastembed", "semantic embeddings (hash fallback otherwise)"),
        _check_service(
            "RabbitMQ",
            os.getenv("MYTHOS_BROKER_URL", "amqp://mythos:mythos@localhost:5672/"),
            5672,
            "start with `docker compose up -d` or use --bus inmemory",
        ),
        _check_service(
            "Qdrant",
            os.getenv("MYTHOS_QDRANT_URL", "http://localhost:6333"),
            6333,
            "start with `docker compose up -d` or use --matrix inmemory",
        ),
    ]

    if os.getenv("ORS_API_KEY") or os.getenv("MYTHOS_ORS_URL"):
        results.append(CheckResult("navigator (openrouteservice)", OK, "configured"))
    else:
        results.append(CheckResult(
            "navigator (openrouteservice)", WARN,
            "no ORS_API_KEY / MYTHOS_ORS_URL - the navigator role will refuse tasks",
        ))

    tts = os.getenv("MYTHOS_TTS_URL", "")
    if tts:
        parsed = urllib.parse.urlparse(tts)
        results.append(_check_service(
            "voice (TTS sidecar)", tts, parsed.port or 8000,
            "start with `docker compose --profile voice up -d`",
        ))
    else:
        results.append(CheckResult(
            "voice (TTS sidecar)", WARN,
            "no MYTHOS_TTS_URL - the voice role will refuse tasks",
        ))
    return results


def format_report(results: List[CheckResult]) -> str:
    width = max(len(r.name) for r in results)
    lines = ["Mythos doctor", "-" * (width + 40)]
    for r in results:
        lines.append(f"[{r.status:^4}] {r.name.ljust(width)}  {r.detail}")
    fails = sum(1 for r in results if r.status == FAIL)
    warns = sum(1 for r in results if r.status == WARN)
    lines.append("-" * (width + 40))
    if fails:
        lines.append(f"{fails} blocking problem(s), {warns} optional capability warning(s).")
    else:
        lines.append(f"Ready. {warns} optional capability warning(s).")
    return "\n".join(lines)


def doctor_exit_code(results: List[CheckResult]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0
