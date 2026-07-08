"""
mythos/memory.py
----------------
Short-term and long-term memory management for the Mythos agent.

Short-term memory  – a sliding window of recent messages/observations.
Long-term memory   – a persistent key-value scratchpad the agent can read/write.

Messages are stored provider-neutrally.  A message can be a plain
system/user/assistant turn, an assistant turn that *makes* a tool call
(carrying ``tool_name`` / ``tool_args`` / ``tool_call_id``), or a tool-result
turn (carrying ``name`` / ``tool_call_id``).  Each LLM provider translates this
neutral representation into its own wire format (see ``mythos/llm.py``).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """A single message in the agent's conversation history."""
    role: str          # "system" | "user" | "assistant" | "tool"
    content: str
    name: Optional[str] = None            # tool name when role == "tool"
    tool_name: Optional[str] = None       # tool invoked by an assistant turn
    tool_args: Optional[Dict[str, Any]] = None  # arguments for that invocation
    tool_call_id: Optional[str] = None    # links an assistant tool call to its result

    @property
    def has_tool_call(self) -> bool:
        return self.tool_name is not None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_name is not None:
            d["tool_name"] = self.tool_name
            d["tool_args"] = self.tool_args or {}
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        return d


# ---------------------------------------------------------------------------
# Short-term memory
# ---------------------------------------------------------------------------

class ShortTermMemory:
    """
    A rolling window of the most recent agent messages.

    When the window is full the oldest non-system messages are evicted to keep
    the context within the LLM's token budget.  Eviction preserves the
    integrity of tool-calling exchanges: an assistant turn that makes a tool
    call is always evicted together with its tool-result turn(s), and any
    orphaned leading tool-result is dropped so the resulting history is never
    wire-invalid for the Anthropic / OpenAI APIs (which reject a tool result
    that is not preceded by the matching tool call).
    """

    def __init__(self, window: int = 20) -> None:
        self._window = window
        self._messages: List[Message] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, message: Message) -> None:
        """Append a message and enforce the window limit."""
        self._messages.append(message)
        self._evict()

    def get_all(self) -> List[Message]:
        """Return all stored messages (system first, then chronological)."""
        return list(self._messages)

    def to_dicts(self) -> List[Dict[str, Any]]:
        """Return messages as plain dicts suitable for an LLM API call."""
        return [m.to_dict() for m in self._messages]

    def clear(self) -> None:
        """Remove all non-system messages (keeps system prompt)."""
        self._messages = [m for m in self._messages if m.role == "system"]

    def reset(self) -> None:
        """Remove every message, including the system prompt."""
        self._messages = []

    def __len__(self) -> int:
        return len(self._messages)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _non_system_count(self) -> int:
        return sum(1 for m in self._messages if m.role != "system")

    def _first_non_system_index(self) -> Optional[int]:
        for i, m in enumerate(self._messages):
            if m.role != "system":
                return i
        return None

    def _evict(self) -> None:
        """Trim oldest non-system messages, keeping tool exchanges intact."""
        evicted = False
        while self._non_system_count() > self._window:
            idx = self._first_non_system_index()
            if idx is None:
                break
            removed = self._messages.pop(idx)
            evicted = True
            # An assistant tool call and its result(s) form one exchange – if we
            # drop the call, drop the paired results so no orphan remains.
            if removed.has_tool_call:
                while idx < len(self._messages) and self._messages[idx].role == "tool":
                    self._messages.pop(idx)
        # Only repair after real eviction: a caller may deliberately add a
        # standalone tool message, and we must not silently discard it.
        if evicted:
            self._drop_leading_orphans()

    def _drop_leading_orphans(self) -> None:
        """Drop any leading tool-result whose originating call was evicted."""
        idx = self._first_non_system_index()
        while idx is not None and self._messages[idx].role == "tool":
            self._messages.pop(idx)
            idx = self._first_non_system_index()


# ---------------------------------------------------------------------------
# Long-term memory (key-value scratchpad)
# ---------------------------------------------------------------------------

class LongTermMemory:
    """
    Persistent key-value scratchpad.

    Values are arbitrary JSON-serialisable objects.  The scratchpad is
    optionally backed by a file on disk so that knowledge is preserved
    across agent runs.
    """

    def __init__(self, persist: bool = False, path: str = "mythos_memory.json") -> None:
        self._store: Dict[str, Any] = {}
        self._persist = persist
        self._path = path

        if persist and os.path.exists(path):
            self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        """Store a value under *key*."""
        self._store[key] = value
        if self._persist:
            self._save()

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve the value for *key*, or *default* if not found."""
        return self._store.get(key, default)

    def delete(self, key: str) -> None:
        """Remove *key* from the store."""
        self._store.pop(key, None)
        if self._persist:
            self._save()

    def keys(self) -> List[str]:
        """Return all keys currently in the store."""
        return list(self._store.keys())

    def snapshot(self) -> Dict[str, Any]:
        """Return a copy of the entire store."""
        return dict(self._store)

    def clear(self) -> None:
        """Remove all entries."""
        self._store.clear()
        if self._persist:
            self._save()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _save(self) -> None:
        # Serialise first so a non-serialisable value cannot leave a truncated
        # file behind, then write atomically via a temp file + rename.
        try:
            payload = json.dumps(self._store, indent=2, default=str)
        except (TypeError, ValueError):
            return  # non-fatal: memory still works in-process
        try:
            directory = os.path.dirname(os.path.abspath(self._path))
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mythos_mem_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except OSError:
            pass  # non-fatal: memory will still work in-process

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._store = data
        except (OSError, json.JSONDecodeError):
            pass


# ---------------------------------------------------------------------------
# Composite memory façade
# ---------------------------------------------------------------------------

class Memory:
    """Unified memory interface exposed to the rest of the agent."""

    def __init__(self, window: int = 20, persist: bool = False, path: str = "mythos_memory.json") -> None:
        self.short = ShortTermMemory(window=window)
        self.long = LongTermMemory(persist=persist, path=path)

    # Convenience pass-throughs for short-term
    def add_message(
        self,
        role: str,
        content: str,
        name: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        tool_call_id: Optional[str] = None,
    ) -> None:
        self.short.add(Message(
            role=role,
            content=content,
            name=name,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=tool_call_id,
        ))

    def get_messages(self) -> List[Dict[str, Any]]:
        return self.short.to_dicts()

    def clear_short_term(self) -> None:
        self.short.clear()

    def reset_short_term(self) -> None:
        """Drop the entire short-term history, including any system prompt."""
        self.short.reset()
