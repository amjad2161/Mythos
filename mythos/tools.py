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

import datetime
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


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
        return tool.call(**arguments)


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------

def _tool_current_time() -> str:
    """Return the current UTC date and time."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _tool_calculate(expression: str) -> str:
    """
    Safely evaluate a mathematical expression.

    Only numeric operations and math functions are permitted.
    """
    allowed_names: Dict[str, Any] = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    allowed_names["abs"] = abs
    allowed_names["round"] = round
    try:
        result = eval(expression, {"__builtins__": {}}, allowed_names)  # noqa: S307
        return str(result)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


def _tool_read_file(path: str) -> str:
    """Read and return the contents of a text file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
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
        return "\n".join(lines) if lines else "(empty directory)"
    except OSError as exc:
        return f"ERROR: {exc}"


def _tool_run_shell(command: str, timeout: int = 30) -> str:
    """
    Execute a shell command and return combined stdout/stderr output.

    The command runs in a subprocess with a configurable timeout (default 30 s).
    Dangerous operations (rm -rf /, etc.) are not prevented at this layer –
    the agent's goal-alignment is expected to avoid destructive actions.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,          # noqa: S602
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {timeout} seconds"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


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
