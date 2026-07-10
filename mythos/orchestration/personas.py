"""
mythos/orchestration/personas.py
--------------------------------
Persona definitions for swarm agents.

A persona is a Markdown file with a strict frontmatter block (the schema is
modeled on the agency-agents persona library: identity, mission, hard rules,
success metrics) followed by a free-form body.  Personas compile into a
system-prompt suffix so each role carries a stable professional identity on
top of the core Mythos prompt.

Format::

    ---
    name: Ada
    role: backend_dev
    mission: Ship working, verified code for every objective.
    rules:
      - Never fabricate file contents or command output.
      - Verify your work by executing it before finishing.
    success_metrics:
      - The critic validates the artifact on the first attempt.
    ---
    Free-form guidance body (optional).

Parsing is a strict, hand-rolled stdlib parser — no YAML dependency.
Built-in personas ship inside the package (``personas/*.md``); a directory
named by ``MYTHOS_PERSONA_DIR`` overlays/overrides them by role.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "personas")
_LIST_KEYS = ("rules", "success_metrics")
_REQUIRED_KEYS = ("name", "role", "mission")


class PersonaError(ValueError):
    """Raised when a persona file cannot be parsed."""


@dataclass
class Persona:
    """A compiled agent persona."""

    name: str
    role: str
    mission: str
    rules: List[str] = field(default_factory=list)
    success_metrics: List[str] = field(default_factory=list)
    body: str = ""

    def compile_system_suffix(self) -> str:
        """Render the persona as a system-prompt block."""
        lines = [
            "PERSONA",
            "-------",
            f"You are {self.name}, the swarm's {self.role} agent.",
            f"Mission: {self.mission}",
        ]
        if self.rules:
            lines.append("Hard rules:")
            lines.extend(f"{i}. {rule}" for i, rule in enumerate(self.rules, 1))
        if self.success_metrics:
            lines.append("You succeed when:")
            lines.extend(f"- {metric}" for metric in self.success_metrics)
        if self.body.strip():
            lines.append(self.body.strip())
        return "\n".join(lines)


def parse_persona(text: str) -> Persona:
    """Parse one persona document; raise ``PersonaError`` when malformed."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PersonaError("persona must start with a '---' frontmatter block")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise PersonaError("unterminated frontmatter block (missing closing '---')") from None

    fields: Dict[str, str] = {}
    lists: Dict[str, List[str]] = {key: [] for key in _LIST_KEYS}
    current_list: Optional[str] = None

    for raw in lines[1:end]:
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current_list is None:
                raise PersonaError(f"list item outside a list key: {stripped!r}")
            lists[current_list].append(stripped[2:].strip())
            continue
        if ":" not in stripped:
            raise PersonaError(f"malformed frontmatter line: {stripped!r}")
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if key in _LIST_KEYS:
            if value:
                raise PersonaError(f"'{key}' must be a '- item' list, got inline value")
            current_list = key
        else:
            fields[key] = value
            current_list = None

    for key in _REQUIRED_KEYS:
        if not fields.get(key):
            raise PersonaError(f"missing required persona field '{key}'")

    return Persona(
        name=fields["name"],
        role=fields["role"],
        mission=fields["mission"],
        rules=lists["rules"],
        success_metrics=lists["success_metrics"],
        body="\n".join(lines[end + 1:]),
    )


def load_personas(directory: str) -> Dict[str, Persona]:
    """Load every ``*.md`` persona in *directory*, keyed by role."""
    personas: Dict[str, Persona] = {}
    if not os.path.isdir(directory):
        return personas
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".md"):
            continue
        path = os.path.join(directory, entry)
        with open(path, "r", encoding="utf-8") as fh:
            try:
                persona = parse_persona(fh.read())
            except PersonaError as exc:
                raise PersonaError(f"{path}: {exc}") from exc
        if persona.role in personas:
            raise PersonaError(f"{path}: duplicate persona for role '{persona.role}'")
        personas[persona.role] = persona
    return personas


def builtin_personas(override_dir: str = "") -> Dict[str, Persona]:
    """
    Packaged personas, with *override_dir* (or ``MYTHOS_PERSONA_DIR``)
    layered on top — an override with the same role wins.
    """
    personas = load_personas(_BUILTIN_DIR)
    override_dir = override_dir or os.getenv("MYTHOS_PERSONA_DIR", "")
    if override_dir:
        personas.update(load_personas(override_dir))
    return personas


def get_persona(role: str, override_dir: str = "") -> Optional[Persona]:
    """Convenience lookup of a single role's persona."""
    return builtin_personas(override_dir).get(role)
