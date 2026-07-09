"""
mythos/orchestration/roles.py
-----------------------------
Per-role Tools APIs.

Each agent role gets its own tool registry – the vision's "every agent has a
Tools API defined for it".  Registries are built by filtering the single-agent
``build_default_registry`` down to a role's allow-list, then removing any
tools the TaskPayload's ``constraints.forbidden_modules`` bans for the
specific task.

Note the deliberate asymmetry: the critic's registry is read/execute only –
it verifies work, it never fixes it.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

from ..tools import ToolRegistry, build_default_registry

# Allow-lists per role.  `finish` is mandatory everywhere – it is how an
# agent's inner loop terminates.
ROLE_TOOLS: Dict[str, List[str]] = {
    "backend_dev": [
        "read_file",
        "write_file",
        "append_file",
        "list_directory",
        "run_shell",
        "calculate",
        "current_time",
        "think",
        "finish",
    ],
    "critic": [
        "read_file",
        "list_directory",
        "run_shell",
        "current_time",
        "think",
        "finish",
    ],
    # Web research: fetch + files, deliberately NO shell access.
    "researcher": [
        "web_fetch",
        "read_file",
        "write_file",
        "list_directory",
        "current_time",
        "think",
        "finish",
    ],
    # Geographic intelligence over the openrouteservice API.
    "navigator": [
        "ors_geocode",
        "ors_directions",
        "ors_isochrones",
        "ors_matrix",
        "calculate",
        "read_file",
        "write_file",
        "current_time",
        "think",
        "finish",
    ],
    # Text-to-speech artifacts via the TTS sidecar.
    "voice": [
        "speak",
        "read_file",
        "write_file",
        "list_directory",
        "current_time",
        "think",
        "finish",
    ],
}


def known_roles() -> List[str]:
    return sorted(ROLE_TOOLS.keys())


def build_registry_for_role(
    role: str,
    forbidden_modules: Sequence[str] = (),
) -> ToolRegistry:
    """
    Build the Tools API for *role*, minus any per-task forbidden tools.

    Raises ``ValueError`` for an unknown role – a payload addressed to a role
    nobody defined must fail loudly, not fall back to the full toolset.
    """
    allowed = ROLE_TOOLS.get(role)
    if allowed is None:
        raise ValueError(f"Unknown agent role: '{role}'. Known roles: {known_roles()}")

    banned = set(forbidden_modules)
    if "finish" in banned:
        # Without `finish` the inner loop can only end via the iteration cap.
        banned.discard("finish")

    defaults = build_default_registry()
    registry = ToolRegistry()
    for name in allowed:
        if name in banned:
            continue
        tool = defaults.get(name)
        if tool is None:
            # A role allow-list naming a tool that doesn't exist is a wiring
            # bug (typo/rename) – fail at startup, not as N confusing LLM
            # retries from a silently under-tooled worker.
            raise ValueError(
                f"Role '{role}' lists unknown tool '{name}' "
                f"(not in the default registry)"
            )
        registry.register(tool)
    return registry
