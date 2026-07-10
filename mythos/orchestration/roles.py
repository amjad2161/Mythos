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
    # Speech in and out via the TTS/ASR sidecars.
    "voice": [
        "speak",
        "transcribe",
        "read_file",
        "write_file",
        "list_directory",
        "current_time",
        "think",
        "finish",
    ],
    # Digital secretary: tasks, notes, reminders, e-mail drafts, briefings.
    # Read-only web lookups allowed; deliberately NO shell/OS control.
    "assistant": [
        "pa_add_task",
        "pa_list_tasks",
        "pa_complete_task",
        "pa_add_note",
        "pa_list_notes",
        "pa_set_reminder",
        "pa_due_reminders",
        "pa_draft_email",
        "pa_daily_brief",
        "web_fetch",
        "read_file",
        "write_file",
        "current_time",
        "think",
        "finish",
    ],
    # Web use: real browser automation (navigate, indexed-DOM read, click,
    # fill, screenshot). Untrusted-page input role: NO shell.
    "browser": [
        "browser_navigate",
        "browser_read_page",
        "browser_click",
        "browser_fill",
        "browser_screenshot",
        "web_fetch",
        "read_file",
        "write_file",
        "current_time",
        "think",
        "finish",
    ],
    # Computer use: control the desktop (open, clipboard, notify, screenshot).
    # Untrusted-screen input role: NO shell, NO file writes beyond screenshots.
    "operator": [
        "open_url",
        "open_path",
        "clipboard_get",
        "clipboard_set",
        "notify",
        "screenshot",
        "computer_move",
        "computer_click",
        "computer_type",
        "computer_key",
        "computer_scroll",
        "read_file",
        "list_directory",
        "current_time",
        "think",
        "finish",
    ],
}


def known_roles() -> List[str]:
    return sorted(ROLE_TOOLS.keys())


# Access levels (TargetAgent.access_level) gate what a role's registry may
# mutate, independent of the role itself:
#   restricted – read/reason only: every state-mutating tool is stripped
#   standard   – the role's full allow-list (default)
#   elevated   – reserved for Phase C privileged flows; = standard for now
ACCESS_LEVELS = ("restricted", "standard", "elevated")
# Outward/state-mutating tools stripped at the `restricted` access level.  For
# the operator role this leaves perception-only capability (screenshot +
# clipboard_get), matching the untrusted-input containment in the blueprint.
_MUTATING_TOOLS = frozenset({
    "run_shell",
    "write_file",
    "append_file",
    "speak",
    "open_url",
    "open_path",
    "clipboard_set",
    "notify",
    "browser_navigate",
    "browser_click",
    "browser_fill",
    "computer_move",
    "computer_click",
    "computer_type",
    "computer_key",
    "computer_scroll",
})


def build_registry_for_role(
    role: str,
    forbidden_modules: Sequence[str] = (),
    access_level: str = "standard",
) -> ToolRegistry:
    """
    Build the Tools API for *role*, minus any per-task forbidden tools,
    filtered by the payload's *access_level*.

    Raises ``ValueError`` for an unknown role or access level – a payload
    addressed to a role/level nobody defined must fail loudly, not fall back
    to the full toolset.
    """
    allowed = ROLE_TOOLS.get(role)
    if allowed is None:
        raise ValueError(f"Unknown agent role: '{role}'. Known roles: {known_roles()}")
    if access_level not in ACCESS_LEVELS:
        raise ValueError(
            f"Unknown access level: '{access_level}'. Known: {list(ACCESS_LEVELS)}"
        )

    banned = set(forbidden_modules)
    if access_level == "restricted":
        banned |= _MUTATING_TOOLS
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
