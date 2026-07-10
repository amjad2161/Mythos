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
import os
import urllib.error
import urllib.request
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
    usage : dict
        Token accounting normalised across providers.  Canonical keys:
        ``input``, ``output``, ``cache_read``, ``cache_creation`` (0 when the
        provider does not report a figure; empty dict for scripted stubs).
    """

    def __init__(
        self,
        content: Optional[str],
        tool_name: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        tool_call_id: Optional[str] = None,
        raw: Any = None,
        usage: Optional[Dict[str, int]] = None,
    ) -> None:
        self.content = content
        self.tool_name = tool_name
        self.tool_args: Dict[str, Any] = tool_args or {}
        self.tool_call_id = tool_call_id
        self.raw = raw
        self.usage: Dict[str, int] = usage or {}

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
        usage = _openai_usage(response)

        if msg.tool_calls:
            tc = msg.tool_calls[0]
            return LLMResponse(
                content=msg.content,
                tool_name=tc.function.name,
                tool_args=_safe_json_args(tc.function.arguments),
                tool_call_id=tc.id,
                raw=response,
                usage=usage,
            )

        return LLMResponse(content=msg.content, raw=response, usage=usage)


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

class AnthropicLLM(BaseLLM):
    """Anthropic Claude provider."""

    def __init__(
        self,
        model: str,
        api_key: Optional[str],
        cache_system_prompt: bool = True,
    ) -> None:
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
        # Escape hatch for SDKs/deployments that reject cache_control blocks.
        self._cache_system_prompt = cache_system_prompt

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
            if self._cache_system_prompt:
                # Prompt caching: the system prompt is the stable prefix of
                # every request in an agent loop; below the provider's minimum
                # cacheable size the marker is simply a no-op.
                kwargs["system"] = [{
                    "type": "text",
                    "text": system_msg,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                kwargs["system"] = system_msg
        if ant_tools:
            kwargs["tools"] = ant_tools
            # One tool call per turn keeps the executor's single-call loop valid.
            kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

        response = self._client.messages.create(**kwargs)
        usage = _anthropic_usage(response)

        # A safety refusal is a successful HTTP 200 with stop_reason "refusal";
        # surface it as text rather than treating an empty content list as a bug.
        if getattr(response, "stop_reason", None) == "refusal":
            return LLMResponse(
                content="[The model declined to respond to this request.]",
                raw=response,
                usage=usage,
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
                usage=usage,
            )

        return LLMResponse(content=text or "", raw=response, usage=usage)


# ---------------------------------------------------------------------------
# Local / free provider (OpenAI-compatible, dependency-free)
# ---------------------------------------------------------------------------

class LocalLLM(BaseLLM):
    """
    Local / free-model provider over any OpenAI-compatible chat endpoint.

    Pure ``urllib`` — no SDK dependency — so Mythos can run entirely on a
    local model (Ollama, LM Studio, llama.cpp, vLLM) or a free hosted gateway
    (Groq, Together, OpenRouter) by pointing at its ``/v1`` base URL.  The
    default targets Ollama's OpenAI-compatible server on ``localhost:11434``.

    Configuration (env, read here so ``MythosConfig`` stays provider-neutral):
      * ``MYTHOS_LOCAL_URL``     base URL incl. ``/v1`` (default Ollama).
      * ``MYTHOS_LOCAL_API_KEY`` bearer token; most local servers ignore it
        (default ``"local"``), hosted gateways require their key.

    The wire format is identical to :class:`OpenAILLM`, so the same message and
    tool-spec translation is reused.  Models that don't support tool-calling
    simply return plain content — the executor's plain-text nudge handles that.
    """

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        base = (base_url or os.getenv("MYTHOS_LOCAL_URL", "http://localhost:11434/v1"))
        self._url = base.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key or os.getenv("MYTHOS_LOCAL_API_KEY", "local")
        # A malformed timeout env var is a config typo, not a reason to refuse
        # to start — fall back to the default rather than raising.
        try:
            self._timeout = max(1, int(os.getenv("MYTHOS_LOCAL_TIMEOUT_S", "120")))
        except ValueError:
            self._timeout = 120

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": _to_openai_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            # Match OpenAILLM: one tool call per turn so nothing is silently
            # dropped (we only consume tool_calls[0]). Harmless if the backend
            # ignores the flag.
            payload["parallel_tool_calls"] = False

        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            raise RuntimeError(f"Local LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            # Surface as a connection error so RetryingLLM treats it transient.
            raise RuntimeError(f"Local LLM connection failed: {exc.reason}") from exc

        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = _local_usage(body.get("usage"))

        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            call = tool_calls[0]
            fn = call.get("function") or {}
            return LLMResponse(
                content=msg.get("content"),
                tool_name=fn.get("name"),
                tool_args=_safe_json_args(fn.get("arguments")),
                tool_call_id=call.get("id"),
                raw=body,
                usage=usage,
            )
        return LLMResponse(content=msg.get("content") or "", raw=body, usage=usage)


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
# Retry decorator
# ---------------------------------------------------------------------------

# Substrings identifying transient provider failures worth retrying (rate
# limits, overload, connection blips).  Matched case-insensitively against
# the exception's type name and message.
_TRANSIENT_MARKERS = (
    "ratelimit", "rate limit", "overloaded", "apiconnection", "connection",
    "timeout", "timed out", "internalserver", "529", "503", "502",
)


class RetryingLLM(BaseLLM):
    """
    Wraps any ``BaseLLM`` with exponential-backoff retries on transient
    provider errors.  Non-transient errors and exhausted retries re-raise.
    """

    def __init__(
        self,
        inner: BaseLLM,
        attempts: int = 3,
        base_delay: float = 1.0,
        jitter: float = 0.5,
    ) -> None:
        self._inner = inner
        self._attempts = max(1, attempts)
        self._base_delay = base_delay
        self._jitter = jitter

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        import random  # noqa: PLC0415
        import time  # noqa: PLC0415

        last_exc: Optional[Exception] = None
        for attempt in range(self._attempts):
            try:
                return self._inner.chat(
                    messages, tools=tools, temperature=temperature, max_tokens=max_tokens
                )
            except Exception as exc:  # noqa: BLE001
                if not _is_transient(exc) or attempt == self._attempts - 1:
                    raise
                last_exc = exc
                time.sleep(
                    self._base_delay * (2 ** attempt)
                    + random.uniform(0, self._jitter)  # noqa: S311 – jitter, not crypto
                )
        raise last_exc  # pragma: no cover – loop always returns or raises


def _is_transient(exc: Exception) -> bool:
    haystack = f"{type(exc).__name__} {exc}".lower()
    return any(marker in haystack for marker in _TRANSIENT_MARKERS)


# ---------------------------------------------------------------------------
# Usage extraction helpers
# ---------------------------------------------------------------------------

def _anthropic_usage(response: Any) -> Dict[str, int]:
    """Normalise an Anthropic response's usage block (defensive getattr)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        "input": int(getattr(usage, "input_tokens", 0) or 0),
        "output": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_creation": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    }


def _openai_usage(response: Any) -> Dict[str, int]:
    """Normalise an OpenAI response's usage block."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        "input": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output": int(getattr(usage, "completion_tokens", 0) or 0),
        "cache_read": 0,
        "cache_creation": 0,
    }


def _local_usage(usage: Any) -> Dict[str, int]:
    """Normalise a JSON usage block from an OpenAI-compatible local server."""
    if not isinstance(usage, dict):
        return {}
    return {
        "input": int(usage.get("prompt_tokens", 0) or 0),
        "output": int(usage.get("completion_tokens", 0) or 0),
        "cache_read": 0,
        "cache_creation": 0,
    }


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
    """
    Translate neutral history into OpenAI Chat Completions wire messages.

    When ``tool_call_id`` is missing from neutral history, the id synthesized
    for an assistant tool call is remembered and reused for the following tool
    result, so the two halves stay linked.  A tool result with no id and no
    preceding tool call cannot be made wire-valid and is dropped.
    """
    out: List[Dict[str, Any]] = []
    fallback_counter = 0
    last_tool_call_id: Optional[str] = None
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_name") is not None:
            call_id = m.get("tool_call_id")
            if not call_id:
                call_id = f"call_fb_{fallback_counter}"
                fallback_counter += 1
            last_tool_call_id = call_id
            out.append({
                "role": "assistant",
                "content": m.get("content") or None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": m["tool_name"],
                        "arguments": json.dumps(m.get("tool_args") or {}),
                    },
                }],
            })
        elif role == "tool":
            result_id = m.get("tool_call_id") or last_tool_call_id
            if not result_id:
                continue  # orphan tool result: no call to link it to
            out.append({
                "role": "tool",
                "tool_call_id": result_id,
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
    fallback_counter = 0
    last_tool_call_id: Optional[str] = None

    for m in messages:
        role = m.get("role")
        if role == "system":
            system_parts.append(m.get("content", ""))
        elif role == "tool":
            # A synthesized id must match the one used for the originating
            # tool_use block; an orphan result with no known call is dropped
            # rather than emitted with an unlinkable id.
            result_id = m.get("tool_call_id") or last_tool_call_id
            if not result_id:
                continue
            conv.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": result_id,
                    "content": m.get("content", ""),
                }],
            })
        elif role == "assistant" and m.get("tool_name") is not None:
            call_id = m.get("tool_call_id")
            if not call_id:
                call_id = f"call_fb_{fallback_counter}"
                fallback_counter += 1
            last_tool_call_id = call_id
            blocks: List[Dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            blocks.append({
                "type": "tool_use",
                "id": call_id,
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
    if provider in ("local", "ollama"):
        return LocalLLM(model=model, api_key=api_key)
    if provider == "stub":
        return StubLLM()
    raise ValueError(
        f"Unknown LLM provider: '{provider}'. "
        "Choose 'anthropic', 'openai', 'local' (OpenAI-compatible / Ollama), or 'stub'."
    )
