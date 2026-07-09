"""
mythos/orchestration/decomposer.py
----------------------------------
Dynamic goal decomposition (Phase B): two-stage routing.

Stage 1 – deterministic pre-filter: keyword routing narrows the candidate
roles for a goal (cheap, auditable, testable).

Stage 2 – a small/cheap LLM receives the goal plus the candidate roles (with
their tool lists) and must return ONLY a strict JSON object::

    {"steps": [{"role": "...", "objective": "...",
                "validation_command": "", "success_criteria": ""}],
     "rationale": "..."}

Parsing is strict (``SchemaError`` on any deviation); one re-prompt carries
the exact parse error back to the model; a second failure falls back to the
configured deterministic workflow.  The output is an ordinary ``Workflow``
(with ``literal=True`` steps), so dynamic decomposition drives the exact same
TaskPayload/queue pipeline as rigid workflows – no new dispatch machinery.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import MythosConfig
from ..llm import BaseLLM, RetryingLLM, create_llm
from .config import OrchestrationConfig
from .roles import ROLE_TOOLS, known_roles
from .schemas import SchemaError
from .workflows import Workflow, WorkflowStep, get_workflow

# Stage 1: keyword hints per role.  backend_dev is always a candidate – it is
# the general-purpose executor.
ROLE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "navigator": (
        "route", "directions", "geocode", "isochrone", "travel", "distance",
        "map", "navigate", "drive", "commute", "reachable",
    ),
    "voice": ("speak", "spoken", "audio", "tts", "voice", "announce", "narrate"),
    "researcher": (
        "research", "web", "look up", "find out", "compare", "investigate",
        "sources", "summarize the latest",
    ),
}

_ROLE_DESCRIPTIONS: Dict[str, str] = {
    "backend_dev": "general software engineer: writes code/files, runs shell commands",
    "critic": "QA verifier (never assigned directly - reviews are structural)",
    "researcher": "gathers information from the public web and files (no shell)",
    "navigator": "geographic answers: geocoding, routing, travel times, reachability",
    "voice": "turns text into spoken audio files via the TTS service",
}

_DECOMPOSER_SYSTEM = """\
You are a task decomposition engine for a multi-agent system. You receive a
goal and a list of available agent roles. Split the goal into 1..{max_steps}
sequential steps, each assigned to exactly one role.

Respond with ONLY a JSON object - no markdown fences, no prose:
{{"steps": [{{"role": "<role>", "objective": "<what that agent must do>",
"validation_command": "<optional shell command, exit 0 = success, or empty>",
"success_criteria": "<one sentence>",
"depends_on": [<indices of prerequisite steps>]}}],
"rationale": "<one sentence explaining the split>"}}

Rules: use only the listed roles; keep objectives self-contained (each agent
sees only its own objective plus shared memory); prefer fewer steps.
depends_on lists earlier step indices (0-based); use [] for steps that are
independent - independent steps run CONCURRENTLY, so parallelize when the
goal allows it. Omit depends_on for a simple sequential chain."""

_DECOMPOSER_USER = """\
GOAL:
{goal}

AVAILABLE ROLES:
{roles_block}
"""


@dataclass
class DecomposedStep:
    role: str
    objective: str
    validation_command: str = ""
    success_criteria: str = ""
    # Prerequisite step indices; None = sequential (previous step).
    depends_on: Optional[List[int]] = None


def prefilter_roles(goal: str) -> List[str]:
    """Stage 1: deterministic keyword routing (always includes backend_dev)."""
    lowered = goal.lower()
    candidates = ["backend_dev"]
    for role, keywords in ROLE_KEYWORDS.items():
        if role in known_roles() and any(k in lowered for k in keywords):
            candidates.append(role)
    return candidates


def parse_decomposition(
    raw: str, allowed_roles: Sequence[str], max_steps: int
) -> Tuple[List[DecomposedStep], str]:
    """Strictly parse the decomposer's JSON; raise ``SchemaError`` on deviation."""
    if not raw or not raw.strip():
        raise SchemaError("decomposer returned no content")
    text = raw.strip()
    # Tolerate a fenced block – models occasionally add one despite the rules.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SchemaError(f"decomposition is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise SchemaError("decomposition must be an object with a 'steps' array")
    raw_steps = data["steps"]
    if not 1 <= len(raw_steps) <= max_steps:
        raise SchemaError(f"decomposition must contain 1..{max_steps} steps, got {len(raw_steps)}")

    steps: List[DecomposedStep] = []
    for i, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise SchemaError(f"step {i} is not an object")
        role = item.get("role")
        objective = item.get("objective")
        if role not in allowed_roles:
            raise SchemaError(f"step {i} uses role {role!r}, allowed: {sorted(allowed_roles)}")
        if not isinstance(objective, str) or not objective.strip():
            raise SchemaError(f"step {i} has an empty objective")
        depends_on = item.get("depends_on")
        if depends_on is not None:
            if not isinstance(depends_on, list) or not all(
                isinstance(d, int) and 0 <= d < i for d in depends_on
            ):
                raise SchemaError(
                    f"step {i} depends_on must list earlier step indices (0..{i - 1})"
                )
        steps.append(DecomposedStep(
            role=role,
            objective=objective.strip(),
            validation_command=str(item.get("validation_command", "") or ""),
            success_criteria=str(item.get("success_criteria", "") or ""),
            depends_on=depends_on,
        ))
    return steps, str(data.get("rationale", ""))


class DynamicDecomposer:
    """LLM-driven goal decomposition with a deterministic fallback."""

    def __init__(self, llm: BaseLLM, config: OrchestrationConfig) -> None:
        self._llm = llm
        self._config = config

    def decompose(self, goal: str) -> Workflow:
        """Return a Workflow for *goal* (dynamic, or the configured fallback)."""
        candidates = prefilter_roles(goal)
        roles_block = "\n".join(
            f"- {role}: {_ROLE_DESCRIPTIONS.get(role, '')} "
            f"(tools: {', '.join(ROLE_TOOLS[role])})"
            for role in candidates
        )
        messages = [
            {
                "role": "system",
                "content": _DECOMPOSER_SYSTEM.format(
                    max_steps=self._config.decomposer_max_steps
                ),
            },
            {
                "role": "user",
                "content": _DECOMPOSER_USER.format(goal=goal, roles_block=roles_block),
            },
        ]

        last_error = ""
        for attempt in range(2):
            response = self._llm.chat(messages, tools=None, max_tokens=2048)
            try:
                steps, rationale = parse_decomposition(
                    response.content or "",
                    allowed_roles=candidates,
                    max_steps=self._config.decomposer_max_steps,
                )
            except SchemaError as exc:
                last_error = str(exc)
                if attempt == 0:
                    # One re-prompt carrying the exact parse error verbatim.
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                    })
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Your response was rejected: {exc}\n"
                            "Respond again with ONLY the JSON object."
                        ),
                    })
                continue

            if self._config.verbose:
                print(f"[Decomposer] {len(steps)} step(s): {rationale}")
            return Workflow(
                name="dynamic",
                steps=[
                    WorkflowStep(
                        role=s.role,
                        objective_template=s.objective,
                        validation_command_template=s.validation_command,
                        success_criteria=s.success_criteria,
                        literal=True,  # LLM text is literal, never .format()ed
                        depends_on=s.depends_on,
                    )
                    for s in steps
                ],
            )

        fallback = get_workflow(self._config.fallback_workflow)
        if self._config.verbose:
            print(
                f"[Decomposer] Falling back to workflow "
                f"'{fallback.name}' after parse failures: {last_error}"
            )
        return fallback


def create_decomposer_llm(
    config: OrchestrationConfig, agent_config: MythosConfig
) -> BaseLLM:
    """The decomposer's LLM: the cheap routing model with retry wrapping."""
    return RetryingLLM(
        create_llm(
            provider=agent_config.llm_provider,
            model=config.decomposer_model,
            api_key=agent_config.llm_api_key,
        ),
        attempts=config.llm_retry_attempts,
        base_delay=config.llm_retry_base_s,
    )
