"""
mythos/guardrails.py
--------------------
Hard boundaries on what agent tools may touch.

The vision demands strict guardrails: "zero tolerance for deleting critical
system files, denial of access to wrong locations." This module enforces a
deny-list of protected filesystem paths and a set of obviously-destructive
shell patterns, applied to the file and shell tools before they act.

Policy is opt-in via ``MYTHOS_GUARDRAILS`` (default ``on``); the protected
roots can be extended with ``MYTHOS_PROTECTED_PATHS`` (os.pathsep-separated).
Guardrails are a safety net, not a sandbox — real isolation is OS-level
(documented in docs/SECURITY.md).
"""
from __future__ import annotations

import os
import re
from typing import List, Optional

# Filesystem roots that agent tools must never write to or delete under.
_DEFAULT_PROTECTED = (
    "/etc", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/boot", "/dev",
    "/proc", "/sys", "/var/lib", "/var/run", "/root/.ssh",
    # Windows equivalents (harmless no-ops on POSIX).
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
)

# Shell fragments that are destructive at system scope; matched case-insensitively.
# Note: an ordinary `rm -rf ./build` is fine — only system-scope targets are
# blocked (see _ROOT_RM and _SYS_RM), not recursive deletion as such.
_DANGEROUS_SHELL = (
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+.*of=/dev/", re.IGNORECASE),
    re.compile(r":\(\)\s*\{.*\|.*&\s*\}\s*;", re.IGNORECASE),  # fork bomb
    re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),
    re.compile(r"\bchmod\s+-[a-z]*R?\s*0*777\s+/", re.IGNORECASE),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),
)

# `rm` targeting a filesystem root, home, or a protected system directory.
_ROOT_RM = re.compile(r"\brm\b[^|;&]*\s(/|/\*|~|\$HOME)(\s|/|\*|$)", re.IGNORECASE)
_SYS_RM = re.compile(
    r"\brm\b[^|;&]*\s(/etc|/bin|/sbin|/usr|/lib|/lib64|/boot|/dev|/proc|/sys|/var)(\s|/|$)",
    re.IGNORECASE,
)


def _enabled() -> bool:
    return os.getenv("MYTHOS_GUARDRAILS", "on").lower() not in ("off", "0", "false")


def protected_roots() -> List[str]:
    roots = list(_DEFAULT_PROTECTED)
    extra = os.getenv("MYTHOS_PROTECTED_PATHS", "")
    if extra:
        roots.extend(p for p in extra.split(os.pathsep) if p)
    return [os.path.abspath(r) if not r.startswith("C:") else r for r in roots]


def check_path(path: str, *, write: bool) -> Optional[str]:
    """
    Return a refusal reason if *path* is off-limits for a *write*, else None.

    Reads are always permitted (the vision restricts mutation, not inspection);
    only writes/deletes are guarded.
    """
    if not _enabled() or not write:
        return None
    try:
        target = os.path.abspath(path)
    except (ValueError, TypeError):
        return f"invalid path: {path!r}"
    for root in protected_roots():
        root_norm = root.rstrip(os.sep) or os.sep
        if target == root_norm or target.startswith(root_norm + os.sep):
            return f"refusing to write under protected system path '{root}'"
    return None


def check_shell(command: str) -> Optional[str]:
    """Return a refusal reason if *command* looks system-destructive, else None."""
    if not _enabled():
        return None
    for pattern in _DANGEROUS_SHELL:
        if pattern.search(command):
            return f"refusing a destructive shell command (matched {pattern.pattern!r})"
    if _ROOT_RM.search(command) or _SYS_RM.search(command):
        return "refusing to remove a filesystem root / home / system directory"
    return None
