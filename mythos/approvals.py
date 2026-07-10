"""
mythos/approvals.py
-------------------
Human-in-the-loop approval gate for outward / irreversible actions.

The keystone of the JARVIS safety story (see ``docs/JARVIS_BLUEPRINT.md`` §5):
once agents can drive the computer, the browser, and the user's data, the
*commit points* — sending, deleting, purchasing, installing, running shell —
must pause for a human who approves the **effect**, not just a tool name.

Design:
* ``classify_action(tool, args)`` maps a tool call to an ``ActionClass``
  (SAFE / REVERSIBLE / OUTWARD / DESTRUCTIVE) from names + argument content.
* An ``ApprovalPolicy`` names which classes require confirmation
  (default: OUTWARD + DESTRUCTIVE).
* An ``ApprovalGate`` consults a **pluggable approver** callback with a
  human-readable preview and returns allow/deny.

Enforcement is **opt-in and fail-safe by default**: unless ``MYTHOS_APPROVALS``
is on, the gate allows everything (so existing autonomous flows are unchanged).
When enforcement is on, a gated action with no registered approver is *denied*
(fail-closed) unless ``MYTHOS_AUTO_APPROVE`` is set (for CI / trusted runs).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, Optional, Set


class ActionClass(IntEnum):
    SAFE = 0          # read/reason only — never gated
    REVERSIBLE = 1    # local, undoable (write a file, set the clipboard)
    OUTWARD = 2       # leaves the machine / user-visible (send, post, open, notify)
    DESTRUCTIVE = 3   # irreversible / high blast radius (delete, overwrite, install)


# Tool → default class. Anything unlisted is SAFE. run_shell is refined by args.
_DESTRUCTIVE_TOOLS = frozenset({"delete_file", "remove_file"})
_OUTWARD_TOOLS = frozenset({
    "open_url", "open_path", "notify", "speak",
    "mail_send", "send_email", "post", "purchase",
    "browser_navigate", "browser_click", "browser_fill",
    "computer_move", "computer_click", "computer_type", "computer_key", "computer_scroll",
})
_REVERSIBLE_TOOLS = frozenset({
    "write_file", "append_file", "clipboard_set", "pa_draft_email",
})

# Destructive command signatures for run_shell (superset-ish of guardrails).
_DESTRUCTIVE_SHELL = re.compile(
    r"(\brm\s+-\w*[rf]|\brmdir\b|\bmkfs|\bdd\s+if=|\bshutdown\b|\breboot\b|"
    r"pip\s+uninstall|npm\s+uninstall|apt(?:-get)?\s+(?:remove|purge)|"
    r"git\s+push\b.*--force|drop\s+(?:table|database)\b)",
    re.IGNORECASE,
)


@dataclass
class ApprovalRequest:
    tool: str
    args: Dict[str, Any]
    action_class: ActionClass
    preview: str = ""


# An approver receives the request and returns True to allow, False to deny.
Approver = Callable[[ApprovalRequest], bool]


@dataclass
class ApprovalPolicy:
    """Which action classes require human confirmation."""

    gated: Set[ActionClass] = field(
        default_factory=lambda: {ActionClass.OUTWARD, ActionClass.DESTRUCTIVE}
    )

    def requires_confirmation(self, action_class: ActionClass) -> bool:
        return action_class in self.gated


def classify_action(tool: str, args: Optional[Dict[str, Any]] = None) -> ActionClass:
    """Classify a tool call by its name and arguments."""
    args = args or {}
    if tool == "run_shell":
        command = str(args.get("command", ""))
        return ActionClass.DESTRUCTIVE if _DESTRUCTIVE_SHELL.search(command) else ActionClass.OUTWARD
    if tool in _DESTRUCTIVE_TOOLS:
        return ActionClass.DESTRUCTIVE
    if tool in _OUTWARD_TOOLS:
        return ActionClass.OUTWARD
    if tool in _REVERSIBLE_TOOLS:
        return ActionClass.REVERSIBLE
    return ActionClass.SAFE


def _preview(tool: str, args: Dict[str, Any]) -> str:
    """A compact human-readable summary of the effect to be approved."""
    shown = {k: (v if len(str(v)) <= 200 else str(v)[:200] + "…") for k, v in args.items()}
    parts = ", ".join(f"{k}={v!r}" for k, v in shown.items())
    return f"{tool}({parts})"


class ApprovalGate:
    """Gate that consults an approver for actions the policy flags."""

    def __init__(
        self,
        approver: Optional[Approver] = None,
        policy: Optional[ApprovalPolicy] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self._approver = approver
        self._policy = policy or ApprovalPolicy()
        # Explicit flag wins; otherwise read the env (default off).
        self._enabled = (
            enabled if enabled is not None
            else os.getenv("MYTHOS_APPROVALS", "off").strip().lower() in ("1", "on", "true")
        )

    def set_approver(self, approver: Optional[Approver]) -> None:
        self._approver = approver

    def check(self, tool: str, args: Optional[Dict[str, Any]] = None) -> "GateResult":
        """Return whether *tool* may run, and why."""
        args = args or {}
        action_class = classify_action(tool, args)
        if not self._enabled or not self._policy.requires_confirmation(action_class):
            return GateResult(True, action_class, "not gated")

        request = ApprovalRequest(tool, args, action_class, _preview(tool, args))
        if self._approver is not None:
            try:
                approved = bool(self._approver(request))
            except Exception:  # noqa: BLE001 – a broken approver denies, never crashes
                return GateResult(False, action_class, "approver error — denied")
            return GateResult(approved, action_class, "approved" if approved else "denied by human")

        # Enforcement on, but no approver: fail-closed unless auto-approve is set.
        if os.getenv("MYTHOS_AUTO_APPROVE", "").strip().lower() in ("1", "on", "true"):
            return GateResult(True, action_class, "auto-approved")
        return GateResult(False, action_class, "no approver registered — denied (fail-closed)")


@dataclass
class GateResult:
    allowed: bool
    action_class: ActionClass
    reason: str


# Process-wide default gate consulted by ToolRegistry.call. Off unless
# MYTHOS_APPROVALS is set; register an approver to enforce interactively.
_DEFAULT_GATE = ApprovalGate()


def default_gate() -> ApprovalGate:
    return _DEFAULT_GATE


def set_approver(approver: Optional[Approver]) -> None:
    """Register the process-wide approver (a UI/CLI prompt, a policy, …)."""
    _DEFAULT_GATE.set_approver(approver)


def guard(tool: str, args: Optional[Dict[str, Any]] = None) -> GateResult:
    """Convenience: consult the process-wide gate."""
    return _DEFAULT_GATE.check(tool, args)
