"""
mythos/memory.py
----------------
Short-term and long-term memory management for the Mythos agent.

Short-term memory  – a sliding window of recent messages/observations.
Long-term memory   – a persistent key-value scratchpad the agent can read/write.
"""
from __future__ import annotations

import json
import os
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
    name: Optional[str] = None   # tool name when role == "tool"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        return d


# ---------------------------------------------------------------------------
# Short-term memory
# ---------------------------------------------------------------------------

class ShortTermMemory:
    """
    A rolling window of the most recent agent messages.

    When the window is full the oldest non-system messages are evicted to
    keep the context within the LLM's token budget.
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

    def __len__(self) -> int:
        return len(self._messages)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _evict(self) -> None:
        """Remove the oldest non-system message when over the limit."""
        non_system = [m for m in self._messages if m.role != "system"]
        while len(non_system) > self._window:
            # find and remove the first non-system message
            for i, m in enumerate(self._messages):
                if m.role != "system":
                    self._messages.pop(i)
                    break
            non_system = [m for m in self._messages if m.role != "system"]


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
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._store, fh, indent=2)
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
    def add_message(self, role: str, content: str, name: Optional[str] = None) -> None:
        self.short.add(Message(role=role, content=content, name=name))

    def get_messages(self) -> List[Dict[str, Any]]:
        return self.short.to_dicts()

    def clear_short_term(self) -> None:
        self.short.clear()
