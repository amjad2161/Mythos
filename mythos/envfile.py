"""
mythos/envfile.py
-----------------
Simple KEY=VALUE environment-file loading for PC installs.

On a personal machine juggling exported environment variables is friction;
Mythos therefore reads configuration from env files at startup:

1. ``~/.mythos/env``   – the per-user config written by ``mythos --init``;
2. ``./.env``          – a project-local override.

Values NEVER override variables already set in the process environment, so
explicit exports and CI configuration always win.  Stdlib only.
"""
from __future__ import annotations

import os
from typing import Dict, List

USER_ENV_PATH = os.path.join(os.path.expanduser("~"), ".mythos", "env")
LOCAL_ENV_PATH = ".env"


def parse_env_file(text: str) -> Dict[str, str]:
    """Parse KEY=VALUE lines ('#' comments and blanks ignored; quotes stripped)."""
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_file(path: str) -> Dict[str, str]:
    """Load *path* into ``os.environ`` (existing variables win). Returns
    the newly applied values ({} when the file doesn't exist)."""
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        values = parse_env_file(fh.read())
    applied: Dict[str, str] = {}
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def load_default_env_files() -> List[str]:
    """Load the standard env files; return the paths that were found."""
    loaded = []
    for path in (USER_ENV_PATH, LOCAL_ENV_PATH):
        if os.path.isfile(path):
            load_env_file(path)
            loaded.append(path)
    return loaded


ENV_TEMPLATE = """\
# Mythos configuration (~/.mythos/env)
# Values here are defaults - exported environment variables always win.

# --- LLM backend -----------------------------------------------------------
# ANTHROPIC_API_KEY=sk-ant-...
# MYTHOS_LLM_MODEL=claude-opus-4-8

# --- Swarm infrastructure (docker compose up -d) ----------------------------
# MYTHOS_BUS=rabbitmq            # or: inmemory (no docker needed)
# MYTHOS_MATRIX=qdrant           # or: inmemory
# MYTHOS_BROKER_URL=amqp://mythos:mythos@localhost:5672/
# MYTHOS_QDRANT_URL=http://localhost:6333

# --- Dynamic orchestration ---------------------------------------------------
# MYTHOS_DYNAMIC=true
# MYTHOS_DECOMPOSER_MODEL=claude-haiku-4-5

# --- Cost governance ---------------------------------------------------------
# MYTHOS_HOURLY_TOKEN_BUDGET=2000000
# MYTHOS_RUN_TOKEN_BUDGET=500000

# --- Domain agents -----------------------------------------------------------
# ORS_API_KEY=...                # navigator (https://openrouteservice.org)
# MYTHOS_TTS_URL=http://localhost:8000   # voice (docker compose --profile voice up)
"""


def write_env_template(path: str = USER_ENV_PATH) -> bool:
    """Write the commented template to *path* unless it already exists.
    Returns True when a new file was created."""
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(ENV_TEMPLATE)
    return True
