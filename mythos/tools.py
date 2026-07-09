"""
mythos/tools.py
---------------
Tool registry and built-in tools for the Mythos autonomous agent.

Tools are the primary way the agent affects the world.  Each tool has:
  - a name
  - a description (shown to the LLM)
  - a JSON-schema for its parameters
  - a callable that performs the action and returns a string result

The ToolRegistry exposes tools both in "function-calling" format for modern
LLM APIs and as a simple callable-by-name dispatch table.
"""
from __future__ import annotations

import ast
import datetime
import math
import operator
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# Guard rails: tool output that flows back into the LLM context is capped so a
# single command (e.g. `cat huge.log`) cannot exhaust the context window.
# read_file shares the same cap — a larger per-tool budget would defeat it.
_MAX_TOOL_OUTPUT_CHARS = 20_000
_MAX_READ_BYTES = _MAX_TOOL_OUTPUT_CHARS


def _truncate(text: str, limit: int = _MAX_TOOL_OUTPUT_CHARS) -> str:
    """Cap *text* to at most *limit* characters, truncation notice included."""
    if len(text) <= limit:
        return text
    # Reserve room for the notice so the final string never exceeds *limit*;
    # len(text) is an upper bound on the omitted count's digit width.
    reserve = len(f"\n… [truncated {len(text)} characters]")
    keep = max(0, limit - reserve)
    suffix = f"\n… [truncated {len(text) - keep} characters]"
    return text[:keep] + suffix if keep else suffix[:limit]


# ---------------------------------------------------------------------------
# Tool descriptor
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    """Descriptor for a single tool."""
    name: str
    description: str
    parameters: Dict[str, Any]          # JSON Schema object
    func: Callable[..., str]
    required: List[str] = field(default_factory=list)

    def to_openai_spec(self) -> Dict[str, Any]:
        """Return the OpenAI function-calling spec for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }

    def call(self, **kwargs: Any) -> str:
        """Invoke the tool with keyword arguments and return a string result."""
        try:
            result = self.func(**kwargs)
            return str(result)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Registry that stores and dispatches tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def openai_specs(self) -> List[Dict[str, Any]]:
        return [t.to_openai_spec() for t in self._tools.values()]

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        tool = self.get(name)
        if tool is None:
            return f"ERROR: Unknown tool '{name}'"
        if not isinstance(arguments, dict):
            return f"ERROR: tool arguments must be an object, got {type(arguments).__name__}"
        return tool.call(**arguments)


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------

def _tool_current_time() -> str:
    """Return the current UTC date and time."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# Whitelisted callables and constants for the calculator.  Only these names are
# reachable — the evaluator walks the AST and rejects anything else, so tricks
# like ``(1).__class__`` or ``__import__('os')`` never execute.
_MATH_FUNCS: Dict[str, Any] = {
    k: getattr(math, k) for k in dir(math) if not k.startswith("_") and callable(getattr(math, k))
}
_MATH_CONSTS: Dict[str, Any] = {
    k: getattr(math, k) for k in dir(math) if not k.startswith("_") and not callable(getattr(math, k))
}
_ALLOWED_FUNCS: Dict[str, Any] = {**_MATH_FUNCS, "abs": abs, "round": round, "min": min, "max": max}
_ALLOWED_CONSTS: Dict[str, Any] = dict(_MATH_CONSTS)

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# Complexity guards: expressions are model-supplied, so a pathological input
# (huge exponents, factorial(10**8), deeply nested ops) must fail fast instead
# of hanging the agent loop.
_MAX_EXPR_CHARS = 10_000
_MAX_AST_NODES = 200
_MAX_POW_EXPONENT = 512
_MAX_INT_BITS = 65_536  # ≈ 20k digits; larger intermediates abort evaluation
_INT_ARG_LIMITS = {"factorial": 5_000, "comb": 10_000, "perm": 10_000}


def _check_size(value: Any) -> Any:
    """Reject integer intermediates too large to keep computing with cheaply."""
    if isinstance(value, int) and value.bit_length() > _MAX_INT_BITS:
        raise ValueError("result too large")
    return value


def _eval_node(node: ast.AST) -> Any:
    """Recursively evaluate a whitelisted arithmetic AST node."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("only numeric literals are allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXPONENT and abs(left) > 1:
            raise ValueError(f"exponent too large (max {_MAX_POW_EXPONENT})")
        return _check_size(_BIN_OPS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTS:
            return _ALLOWED_CONSTS[node.id]
        raise ValueError(f"unknown name: {node.id!r}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise ValueError("only whitelisted math functions may be called")
        if node.keywords:
            raise ValueError("keyword arguments are not supported")
        args = [_eval_node(a) for a in node.args]
        limit = _INT_ARG_LIMITS.get(node.func.id)
        if limit is not None and any(
            isinstance(a, (int, float)) and abs(a) > limit for a in args
        ):
            raise ValueError(f"argument too large for {node.func.id}() (max {limit})")
        return _check_size(_ALLOWED_FUNCS[node.func.id](*args))
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def _tool_calculate(expression: str) -> str:
    """
    Safely evaluate a mathematical expression.

    The expression is parsed to an AST and evaluated with a strict whitelist of
    numeric operators, math functions, and constants.  Attribute access, name
    lookups outside the whitelist, comprehensions, and any other construct are
    rejected, so this cannot be used to reach arbitrary Python objects.
    """
    try:
        if len(expression) > _MAX_EXPR_CHARS:
            return f"ERROR: expression too long (max {_MAX_EXPR_CHARS} characters)"
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
            return f"ERROR: expression too complex (max {_MAX_AST_NODES} AST nodes)"
        return _truncate(str(_eval_node(tree)))
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


def _tool_read_file(path: str) -> str:
    """Read and return the contents of a text file (capped to protect context)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(_MAX_READ_BYTES + 1)
        if len(data) > _MAX_READ_BYTES:
            # Only a bounded prefix is read, so the true file size is unknown;
            # state the cap rather than claim an omitted count we cannot know.
            notice = f"\n… [truncated: file exceeds {_MAX_READ_BYTES} characters]"
            return data[: _MAX_READ_BYTES - len(notice)] + notice
        return data
    except OSError as exc:
        return f"ERROR: {exc}"


def _tool_write_file(path: str, content: str) -> str:
    """Write *content* to *path*, creating directories as needed."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return f"Written {len(content)} characters to '{path}'"
    except OSError as exc:
        return f"ERROR: {exc}"


def _tool_append_file(path: str, content: str) -> str:
    """Append *content* to *path*."""
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(content)
        return f"Appended {len(content)} characters to '{path}'"
    except OSError as exc:
        return f"ERROR: {exc}"


def _tool_list_directory(path: str = ".") -> str:
    """List files and directories at *path*."""
    try:
        entries = os.listdir(path)
        entries.sort()
        lines = []
        for entry in entries:
            full = os.path.join(path, entry)
            kind = "DIR " if os.path.isdir(full) else "FILE"
            lines.append(f"{kind} {entry}")
        return _truncate("\n".join(lines)) if lines else "(empty directory)"
    except OSError as exc:
        return f"ERROR: {exc}"


def run_shell_command(command: str, timeout: int = 30) -> Tuple[int, str]:
    """
    Execute a shell command; return ``(returncode, capped combined output)``.

    Shared by the ``run_shell`` tool and the critic's mechanical validation so
    the two never drift.  Output is always capped with ``_truncate`` – it
    flows back into LLM context either way.  A timeout returns
    ``(-1, "ERROR: ...")``.
    """
    try:
        timeout = max(1, int(timeout))
    except (TypeError, ValueError):
        timeout = 30
    try:
        result = subprocess.run(
            command,
            shell=True,          # noqa: S602
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, f"ERROR: Command timed out after {timeout} seconds"
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode, _truncate(output)


def _tool_run_shell(command: str, timeout: int = 30) -> str:
    """
    Execute a shell command and return combined stdout/stderr output.

    The command runs in a subprocess with a configurable timeout (default 30 s).
    Dangerous operations (rm -rf /, etc.) are not prevented at this layer –
    the agent's goal-alignment is expected to avoid destructive actions.
    """
    try:
        returncode, output = run_shell_command(command, timeout)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"
    if output.startswith("ERROR:"):
        return output
    if returncode != 0:
        output = f"[exit code {returncode}]\n{output}".strip()
    return output if output else "(no output)"


def _tool_memory_store(key: str, value: str) -> str:
    """Store a value in long-term memory under *key*."""
    # The agent has direct access to the registry; this function is a
    # stub – the agent.py wires in a closure with the real memory object.
    return f"(memory_store is not wired yet: key={key})"


def _tool_memory_recall(key: str) -> str:
    """Recall a previously stored value from long-term memory."""
    return f"(memory_recall is not wired yet: key={key})"


def _tool_memory_list() -> str:
    """List all keys in long-term memory."""
    return "(memory_list is not wired yet)"


def _tool_finish(conclusion: str) -> str:
    """
    Mark the current goal as complete and return the final answer/conclusion.

    Calling this tool ends the autonomous loop for the current goal.
    """
    # The agent intercepts this tool call to stop the loop.
    return conclusion


# ---------------------------------------------------------------------------
# Factory – assemble the default registry
# ---------------------------------------------------------------------------

def build_default_registry() -> ToolRegistry:
    """Return a ToolRegistry pre-loaded with the built-in tools."""
    registry = ToolRegistry()

    registry.register(Tool(
        name="current_time",
        description="Return the current UTC date and time.",
        parameters={},
        func=_tool_current_time,
        required=[],
    ))

    registry.register(Tool(
        name="calculate",
        description=(
            "Evaluate a mathematical expression and return the result. "
            "Supports standard arithmetic and all functions from Python's math module."
        ),
        parameters={
            "expression": {
                "type": "string",
                "description": "A valid Python mathematical expression, e.g. '2 ** 10' or 'sqrt(144)'.",
            }
        },
        func=_tool_calculate,
        required=["expression"],
    ))

    registry.register(Tool(
        name="read_file",
        description="Read and return the text contents of a file.",
        parameters={
            "path": {"type": "string", "description": "Absolute or relative file path."}
        },
        func=_tool_read_file,
        required=["path"],
    ))

    registry.register(Tool(
        name="write_file",
        description="Write text content to a file, creating it if it does not exist.",
        parameters={
            "path": {"type": "string", "description": "Absolute or relative file path."},
            "content": {"type": "string", "description": "Text content to write."},
        },
        func=_tool_write_file,
        required=["path", "content"],
    ))

    registry.register(Tool(
        name="append_file",
        description="Append text content to an existing file (or create it).",
        parameters={
            "path": {"type": "string", "description": "Absolute or relative file path."},
            "content": {"type": "string", "description": "Text content to append."},
        },
        func=_tool_append_file,
        required=["path", "content"],
    ))

    registry.register(Tool(
        name="list_directory",
        description="List files and sub-directories inside a directory.",
        parameters={
            "path": {
                "type": "string",
                "description": "Directory path. Defaults to the current working directory.",
                "default": ".",
            }
        },
        func=_tool_list_directory,
        required=[],
    ))

    registry.register(Tool(
        name="run_shell",
        description=(
            "Execute an arbitrary shell command and return its combined stdout/stderr output. "
            "Use with care – prefer specialised tools when they exist."
        ),
        parameters={
            "command": {"type": "string", "description": "Shell command to execute."},
            "timeout": {
                "type": "integer",
                "description": "Maximum execution time in seconds (default: 30).",
                "default": 30,
            },
        },
        func=_tool_run_shell,
        required=["command"],
    ))

    registry.register(Tool(
        name="memory_store",
        description="Persist a string value to long-term memory under a named key.",
        parameters={
            "key": {"type": "string", "description": "Unique identifier for the memory entry."},
            "value": {"type": "string", "description": "The value to store."},
        },
        func=_tool_memory_store,  # replaced at agent init time
        required=["key", "value"],
    ))

    registry.register(Tool(
        name="memory_recall",
        description="Retrieve a previously stored value from long-term memory by key.",
        parameters={
            "key": {"type": "string", "description": "Key to look up."}
        },
        func=_tool_memory_recall,  # replaced at agent init time
        required=["key"],
    ))

    registry.register(Tool(
        name="memory_list",
        description="List all keys currently stored in long-term memory.",
        parameters={},
        func=_tool_memory_list,  # replaced at agent init time
        required=[],
    ))

    registry.register(Tool(
        name="finish",
        description=(
            "Signal that the current goal has been fully achieved. "
            "Provide a concise conclusion/answer as the argument. "
            "ALWAYS call this tool when the goal is complete."
        ),
        parameters={
            "conclusion": {
                "type": "string",
                "description": "Final answer, summary, or result of the completed goal.",
            }
        },
        func=_tool_finish,
        required=["conclusion"],
    ))

    return registry
