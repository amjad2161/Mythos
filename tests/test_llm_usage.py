"""
tests/test_llm_usage.py
-----------------------
Usage extraction, prompt caching, and the RetryingLLM backoff wrapper.
"""
from types import SimpleNamespace

import pytest

from mythos.llm import (
    AnthropicLLM,
    BaseLLM,
    LLMResponse,
    RetryingLLM,
    StubLLM,
    _is_transient,
)


def _install_fake_anthropic(monkeypatch, response):
    """Install a fake `anthropic` module capturing messages.create kwargs."""
    import sys

    rec = {}

    class FakeMessages:
        def create(self, **kwargs):
            rec["kwargs"] = kwargs
            return response

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = FakeMessages()

    fake = SimpleNamespace(Anthropic=FakeClient)
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return rec


class TestUsageExtraction:
    def test_anthropic_usage_extracted(self, monkeypatch):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=120,
                output_tokens=30,
                cache_read_input_tokens=90,
                cache_creation_input_tokens=10,
            ),
        )
        _install_fake_anthropic(monkeypatch, response)
        llm = AnthropicLLM(model="m", api_key="k")
        result = llm.chat([{"role": "user", "content": "hello"}])
        assert result.usage == {
            "input": 120, "output": 30, "cache_read": 90, "cache_creation": 10,
        }

    def test_missing_cache_fields_default_to_zero(self, monkeypatch):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=5, output_tokens=7),
        )
        _install_fake_anthropic(monkeypatch, response)
        result = AnthropicLLM(model="m", api_key="k").chat(
            [{"role": "user", "content": "x"}]
        )
        assert result.usage == {
            "input": 5, "output": 7, "cache_read": 0, "cache_creation": 0,
        }

    def test_stub_default_usage_is_empty(self):
        result = StubLLM().chat([{"role": "user", "content": "x"}])
        assert result.usage == {}

    def test_scripted_response_carries_usage(self):
        stub = StubLLM([LLMResponse(content="hi", usage={"input": 3, "output": 4})])
        assert stub.chat([]).usage == {"input": 3, "output": 4}


class TestRetryingLLM:
    class Flaky(BaseLLM):
        def __init__(self, failures, exc):
            self.calls = 0
            self._failures = failures
            self._exc = exc

        def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
            self.calls += 1
            if self.calls <= self._failures:
                raise self._exc
            return LLMResponse(content="ok")

    def test_transient_error_retried(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("time.sleep", sleeps.append)
        inner = self.Flaky(failures=2, exc=RuntimeError("529 overloaded_error"))
        result = RetryingLLM(inner, attempts=3, base_delay=0.5, jitter=0).chat([])
        assert result.content == "ok"
        assert inner.calls == 3
        assert sleeps == [0.5, 1.0]  # exponential backoff

    def test_non_transient_raises_immediately(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        inner = self.Flaky(failures=5, exc=ValueError("invalid api key"))
        with pytest.raises(ValueError):
            RetryingLLM(inner, attempts=3).chat([])
        assert inner.calls == 1

    def test_exhaustion_reraises_last_error(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        inner = self.Flaky(failures=10, exc=RuntimeError("connection timed out"))
        with pytest.raises(RuntimeError):
            RetryingLLM(inner, attempts=3).chat([])
        assert inner.calls == 3

    def test_transient_classifier(self):
        assert _is_transient(RuntimeError("RateLimitError: slow down"))
        assert _is_transient(ConnectionError("connection reset"))
        assert not _is_transient(ValueError("bad request"))
