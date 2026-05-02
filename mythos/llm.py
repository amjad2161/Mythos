"""
mythos/llm.py
-------------
LLM provider abstraction layer.

Mythos supports multiple backends (OpenAI, Anthropic, and a built-in
stub for offline testing).  All backends expose the same interface so the
rest of the agent code is provider-agnostic.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Response data-type
# ---------------------------------------------------------------------------

class LLMResponse:
    """
    Normalised response returned by every LLM provider.

    Attributes
    ----------
    content : str | None
        Plain-text content of the assistant's message (may be None when
        the model returns only a tool call).
    tool_name : str | None
        Name of the tool the model wants to call (None = no tool call).
    tool_args : dict
        Parsed arguments for the tool call (empty when no tool call).
    raw : Any
        The original provider-specific response object.
    """

    def __init__(
        self,
        content: Optional[str],
        tool_name: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        raw: Any = None,
    ) -> None:
        self.content = content
        self.tool_name = tool_name
        self.tool_args: Dict[str, Any] = tool_args or {}
        self.raw = raw

    @property
    def has_tool_call(self) -> bool:
        return self.tool_name is not None

    def __repr__(self) -> str:
        if self.has_tool_call:
            return f"LLMResponse(tool={self.tool_name}, args={self.tool_args})"
        return f"LLMResponse(content={self.content!r:.80})"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseLLM(ABC):
    """Abstract LLM provider."""

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Send messages to the LLM and return a normalised response."""


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------

class OpenAILLM(BaseLLM):
    """OpenAI Chat Completions provider (including GPT-4o, GPT-4-turbo …)."""

    def __init__(self, model: str, api_key: Optional[str]) -> None:
        try:
            import openai  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "The 'openai' package is required for the OpenAI provider. "
                "Install it with: pip install openai"
            ) from exc

        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = self._client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        # Check for tool call
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            return LLMResponse(
                content=msg.content,
                tool_name=tc.function.name,
                tool_args=json.loads(tc.function.arguments),
                raw=response,
            )

        return LLMResponse(content=msg.content, raw=response)


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

class AnthropicLLM(BaseLLM):
    """Anthropic Claude provider."""

    def __init__(self, model: str, api_key: Optional[str]) -> None:
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "The 'anthropic' package is required for the Anthropic provider. "
                "Install it with: pip install anthropic"
            ) from exc

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        # Anthropic separates system from conversation messages
        system_msg = ""
        conv_msgs: List[Dict[str, Any]] = []
        for m in messages:
            if m["role"] == "system":
                system_msg += m["content"] + "\n"
            else:
                conv_msgs.append(m)

        # Convert OpenAI-style tool specs to Anthropic format
        ant_tools = None
        if tools:
            ant_tools = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"]["description"],
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ]

        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": conv_msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_msg.strip():
            kwargs["system"] = system_msg.strip()
        if ant_tools:
            kwargs["tools"] = ant_tools

        response = self._client.messages.create(**kwargs)

        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use:
            return LLMResponse(
                content=None,
                tool_name=tool_use.name,
                tool_args=tool_use.input,
                raw=response,
            )

        text_block = next((b for b in response.content if b.type == "text"), None)
        return LLMResponse(content=text_block.text if text_block else "", raw=response)


# ---------------------------------------------------------------------------
# Stub provider (offline / testing)
# ---------------------------------------------------------------------------

class StubLLM(BaseLLM):
    """
    A deterministic stub LLM for testing and offline use.

    It cycles through a pre-configured list of responses.  When no
    scripted responses remain it emits a ``finish`` tool call.
    """

    def __init__(self, responses: Optional[List[LLMResponse]] = None) -> None:
        self._responses = list(responses or [])
        self._index = 0

    def add_response(self, response: LLMResponse) -> None:
        self._responses.append(response)

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        if self._index < len(self._responses):
            resp = self._responses[self._index]
            self._index += 1
            return resp
        # Default: finish
        return LLMResponse(
            content=None,
            tool_name="finish",
            tool_args={"conclusion": "Goal completed (stub fallback)."},
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_llm(provider: str, model: str, api_key: Optional[str]) -> BaseLLM:
    """Instantiate the correct LLM backend."""
    provider = provider.lower()
    if provider == "openai":
        return OpenAILLM(model=model, api_key=api_key)
    if provider in ("anthropic", "claude"):
        return AnthropicLLM(model=model, api_key=api_key)
    if provider == "stub":
        return StubLLM()
    raise ValueError(f"Unknown LLM provider: '{provider}'. Choose 'openai', 'anthropic', or 'stub'.")
