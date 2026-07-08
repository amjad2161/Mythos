"""
mythos/llm.py
-------------
LLM provider abstraction layer.

Mythos supports multiple backends (Anthropic Claude, OpenAI, and a built-in
stub for offline testing).  All backends expose the same interface so the rest
of the agent code is provider-agnostic.

The agent stores conversation history in a provider-neutral form (see
``mythos/memory.py``): plain system/user/assistant turns, assistant turns that
make a tool call (``tool_name`` / ``tool_args`` / ``tool_call_id``), and
tool-result turns (``role == "tool"`` with a ``tool_call_id``).  Each provider
below translates that neutral history into its own wire format.  This is the
critical part to get right: both the Anthropic Messages API and the OpenAI Chat
Completions API reject a naive history in which tool results are sent with an
unsupported role or without the id that links them to the originating call.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Response data-type
# ---------------------------------------------------------------------------

class LLMResponse:
    """
    Normalised response returned by every LLM provider.

    Attributes
    ----------
    content : str | None
        Plain-text content of the assistant's message.  May accompany a tool
        call (the model's stated reasoning) or stand alone.
    tool_name : str | None
        Name of the tool the model wants to call (None = no tool call).
    tool_args : dict
        Parsed arguments for the tool call (empty when no tool call).
    tool_call_id : str | None
        Provider-assigned id linking this call to the tool result that answers
        it.  Threaded back into history so the next request is wire-valid.
    raw : Any
        The original provider-specific response object.
    """

    def __init__(
        self,
        content: Optional[str],
        tool_name: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        tool_call_id: Optional[str] = None,
        raw: Any = None,
    ) -> None:
        self.content = content
        self.tool_name = tool_name
        self.tool_args: Dict[str, Any] = tool_args or {}
        self.tool_call_id = tool_call_id
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
            "messages": _to_openai_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
            # Keep the loop single-call per turn so nothing is silently dropped.
            kwargs["parallel_tool_calls"] = False

        response = self._client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        if msg.tool_calls:
            tc = msg.tool_calls[0]
            return LLMResponse(
                content=msg.content,
                tool_name=tc.function.name,
                tool_args=_safe_json_args(tc.function.arguments),
                tool_call_id=tc.id,
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
        system_msg, conv_msgs = _to_anthropic_messages(messages)

        # Convert OpenAI-style tool specs to Anthropic format.
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

        # Note: `temperature` is intentionally NOT sent.  Current Claude models
        # (Opus 4.7/4.8, Sonnet 5, Fable 5) reject sampling parameters with a
        # 400; steer behaviour via prompting instead.
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": conv_msgs,
            "max_tokens": max_tokens,
        }
        if system_msg:
            kwargs["system"] = system_msg
        if ant_tools:
            kwargs["tools"] = ant_tools
            # One tool call per turn keeps the executor's single-call loop valid.
            kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

        response = self._client.messages.create(**kwargs)

        # A safety refusal is a successful HTTP 200 with stop_reason "refusal";
        # surface it as text rather than treating an empty content list as a bug.
        if getattr(response, "stop_reason", None) == "refusal":
            return LLMResponse(
                content="[The model declined to respond to this request.]",
                raw=response,
            )

        text_block = next((b for b in response.content if b.type == "text"), None)
        text = text_block.text if text_block else None

        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use:
            return LLMResponse(
                content=text,
                tool_name=tool_use.name,
                tool_args=dict(tool_use.input) if tool_use.input else {},
                tool_call_id=tool_use.id,
                raw=response,
            )

        return LLMResponse(content=text or "", raw=response)


# ---------------------------------------------------------------------------
# Stub provider (offline / testing)
# ---------------------------------------------------------------------------

class StubLLM(BaseLLM):
    """
    A deterministic stub LLM for testing and offline use.

    It cycles through a pre-configured list of responses.  When no scripted
    responses remain it emits a ``finish`` tool call.
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
# Wire-format translation helpers
# ---------------------------------------------------------------------------

def _safe_json_args(raw: Any) -> Dict[str, Any]:
    """Parse a JSON tool-argument string, tolerating malformed model output."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate neutral history into OpenAI Chat Completions wire messages."""
    out: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_name") is not None:
            out.append({
                "role": "assistant",
                "content": m.get("content") or None,
                "tool_calls": [{
                    "id": m.get("tool_call_id") or f"call_{len(out)}",
                    "type": "function",
                    "function": {
                        "name": m["tool_name"],
                        "arguments": json.dumps(m.get("tool_args") or {}),
                    },
                }],
            })
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id") or f"call_{len(out)}",
                "content": m.get("content", ""),
            })
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


def _to_anthropic_messages(messages: List[Dict[str, Any]]):
    """
    Translate neutral history into (system_prompt, messages) for Anthropic.

    System turns are concatenated into the top-level ``system`` string.  Tool
    results become ``tool_result`` content blocks inside a user turn, and an
    assistant tool call becomes a ``tool_use`` block (with any accompanying
    text preserved), so every request is a valid Messages API payload.
    """
    system_parts: List[str] = []
    conv: List[Dict[str, Any]] = []

    for m in messages:
        role = m.get("role")
        if role == "system":
            system_parts.append(m.get("content", ""))
        elif role == "tool":
            conv.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or f"call_{len(conv)}",
                    "content": m.get("content", ""),
                }],
            })
        elif role == "assistant" and m.get("tool_name") is not None:
            blocks: List[Dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            blocks.append({
                "type": "tool_use",
                "id": m.get("tool_call_id") or f"call_{len(conv)}",
                "name": m["tool_name"],
                "input": m.get("tool_args") or {},
            })
            conv.append({"role": "assistant", "content": blocks})
        else:
            conv.append({"role": role, "content": m.get("content", "")})

    system_msg = "\n".join(p for p in system_parts if p).strip()
    return system_msg, conv


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
