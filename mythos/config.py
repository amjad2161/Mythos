"""
mythos/config.py
----------------
Configuration dataclass for the Mythos agent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MythosConfig:
    """Runtime configuration for the Mythos autonomous agent."""

    # LLM backend settings
    llm_provider: str = "anthropic"       # "anthropic" | "openai" | "stub"
    llm_model: str = "claude-opus-4-5"
    llm_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("MYTHOS_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    )
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096

    # Agent loop settings
    max_iterations: int = 50             # hard cap on autonomous iterations
    max_consecutive_failures: int = 5    # trigger self-recovery after N failures
    reflection_interval: int = 5        # reflect every N iterations

    # Memory settings
    memory_window: int = 20              # how many recent messages to keep in context
    persist_memory: bool = False         # persist memory to disk between runs
    memory_path: str = "mythos_memory.json"

    # Verbosity
    verbose: bool = True

    @classmethod
    def from_env(cls) -> "MythosConfig":
        """Build a config from environment variables."""
        return cls(
            llm_provider=os.getenv("MYTHOS_LLM_PROVIDER", "anthropic"),
            llm_model=os.getenv("MYTHOS_LLM_MODEL", "claude-opus-4-5"),
            llm_api_key=os.getenv("MYTHOS_API_KEY") or os.getenv("ANTHROPIC_API_KEY"),
            llm_temperature=float(os.getenv("MYTHOS_LLM_TEMPERATURE", "0.2")),
            llm_max_tokens=int(os.getenv("MYTHOS_LLM_MAX_TOKENS", "4096")),
            max_iterations=int(os.getenv("MYTHOS_MAX_ITERATIONS", "50")),
            max_consecutive_failures=int(os.getenv("MYTHOS_MAX_FAILURES", "5")),
            reflection_interval=int(os.getenv("MYTHOS_REFLECTION_INTERVAL", "5")),
            memory_window=int(os.getenv("MYTHOS_MEMORY_WINDOW", "20")),
            persist_memory=os.getenv("MYTHOS_PERSIST_MEMORY", "false").lower() == "true",
            memory_path=os.getenv("MYTHOS_MEMORY_PATH", "mythos_memory.json"),
            verbose=os.getenv("MYTHOS_VERBOSE", "true").lower() != "false",
        )
