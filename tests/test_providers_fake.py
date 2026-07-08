"""
tests/test_providers_fake.py
----------------------------
Exercise AnthropicLLM.chat and OpenAILLM.chat without the real SDKs by injecting
fake ``anthropic`` / ``openai`` modules into ``sys.modules``.

This covers the request-assembly logic that cannot otherwise be tested when the
provider packages are not installed: sampling-parameter handling, system-prompt
hoisting, tool-schema conversion, parallel-call disabling, and response parsing.
"""
import sys
import types
from types import SimpleNamespace

import pytest

from mythos.llm import AnthropicLLM, OpenAILLM


# ---------------------------------------------------------------------------
# Fake Anthropic SDK
# ---------------------------------------------------------------------------

def _install_fake_anthropic(monkeypatch, response):
    recorder = {}

    class FakeMessages:
        def create(self, **kwargs):
            recorder["kwargs"] = kwargs
            return response

    class FakeClient:
        def __init__(self, **kwargs):
            recorder["client_kwargs"] = kwargs
            self.messages = FakeMessages()

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return recorder


def _tool_spec():
    return [{
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Do math.",
            "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}},
        },
    }]


class TestAnthropicRequestAssembly:
    def test_temperature_is_not_sent(self, monkeypatch):
        resp = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], stop_reason="end_turn")
        rec = _install_fake_anthropic(monkeypatch, resp)
        llm = AnthropicLLM(model="claude-opus-4-8", api_key="k")
        llm.chat([{"role": "user", "content": "hello"}], temperature=0.9)
        assert "temperature" not in rec["kwargs"]

    def test_system_is_hoisted(self, monkeypatch):
        resp = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], stop_reason="end_turn")
        rec = _install_fake_anthropic(monkeypatch, resp)
        llm = AnthropicLLM(model="claude-opus-4-8", api_key="k")
        llm.chat([
            {"role": "system", "content": "You are Mythos."},
            {"role": "user", "content": "hello"},
        ])
        assert rec["kwargs"]["system"] == "You are Mythos."
        assert all(m["role"] != "system" for m in rec["kwargs"]["messages"])

    def test_tools_converted_and_parallel_disabled(self, monkeypatch):
        resp = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], stop_reason="end_turn")
        rec = _install_fake_anthropic(monkeypatch, resp)
        llm = AnthropicLLM(model="claude-opus-4-8", api_key="k")
        llm.chat([{"role": "user", "content": "hi"}], tools=_tool_spec())
        tool = rec["kwargs"]["tools"][0]
        assert tool["name"] == "calculate"
        assert "input_schema" in tool and "parameters" not in tool
        assert rec["kwargs"]["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}

    def test_tool_use_response_parsed(self, monkeypatch):
        resp = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Let me compute."),
                SimpleNamespace(type="tool_use", id="tu_1", name="calculate", input={"expression": "2**10"}),
            ],
            stop_reason="tool_use",
        )
        _install_fake_anthropic(monkeypatch, resp)
        llm = AnthropicLLM(model="claude-opus-4-8", api_key="k")
        out = llm.chat([{"role": "user", "content": "hi"}], tools=_tool_spec())
        assert out.has_tool_call
        assert out.tool_name == "calculate"
        assert out.tool_args == {"expression": "2**10"}
        assert out.tool_call_id == "tu_1"
        assert out.content == "Let me compute."  # text alongside tool_use preserved

    def test_refusal_is_surfaced(self, monkeypatch):
        resp = SimpleNamespace(content=[], stop_reason="refusal")
        _install_fake_anthropic(monkeypatch, resp)
        llm = AnthropicLLM(model="claude-opus-4-8", api_key="k")
        out = llm.chat([{"role": "user", "content": "hi"}])
        assert not out.has_tool_call
        assert "declined" in out.content.lower()


# ---------------------------------------------------------------------------
# Fake OpenAI SDK
# ---------------------------------------------------------------------------

def _install_fake_openai(monkeypatch, message):
    recorder = {}

    class FakeCompletions:
        def create(self, **kwargs):
            recorder["kwargs"] = kwargs
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            recorder["client_kwargs"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    module = types.ModuleType("openai")
    module.OpenAI = FakeClient
    monkeypatch.setitem(sys.modules, "openai", module)
    return recorder


class TestOpenAIRequestAssembly:
    def test_parallel_tool_calls_disabled_with_tools(self, monkeypatch):
        msg = SimpleNamespace(content="hi", tool_calls=None)
        rec = _install_fake_openai(monkeypatch, msg)
        llm = OpenAILLM(model="gpt-4o", api_key="k")
        llm.chat([{"role": "user", "content": "hi"}], tools=_tool_spec())
        assert rec["kwargs"]["parallel_tool_calls"] is False
        assert rec["kwargs"]["tool_choice"] == "auto"

    def test_tool_call_response_parsed(self, monkeypatch):
        tc = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="calculate", arguments='{"expression": "2**10"}'),
        )
        msg = SimpleNamespace(content=None, tool_calls=[tc])
        _install_fake_openai(monkeypatch, msg)
        llm = OpenAILLM(model="gpt-4o", api_key="k")
        out = llm.chat([{"role": "user", "content": "hi"}], tools=_tool_spec())
        assert out.tool_name == "calculate"
        assert out.tool_args == {"expression": "2**10"}
        assert out.tool_call_id == "call_1"

    def test_malformed_tool_arguments_do_not_crash(self, monkeypatch):
        tc = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="calculate", arguments="not valid json"),
        )
        msg = SimpleNamespace(content=None, tool_calls=[tc])
        _install_fake_openai(monkeypatch, msg)
        llm = OpenAILLM(model="gpt-4o", api_key="k")
        out = llm.chat([{"role": "user", "content": "hi"}], tools=_tool_spec())
        assert out.tool_name == "calculate"
        assert out.tool_args == {}  # falls back to empty dict rather than raising
