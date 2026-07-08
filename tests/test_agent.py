"""
tests/test_agent.py
-------------------
Integration tests driving the full MythosAgent loop with the StubLLM.
"""
import pytest

from mythos.agent import MythosAgent
from mythos.config import MythosConfig
from mythos.llm import LLMResponse, StubLLM
from mythos.tools import Tool, ToolRegistry, build_default_registry


def make_config(**overrides) -> MythosConfig:
    defaults = dict(
        llm_provider="stub",
        llm_api_key="unused",
        verbose=False,
        persist_memory=False,
    )
    defaults.update(overrides)
    return MythosConfig(**defaults)


class TestAgentLoop:
    def test_finish_immediately(self):
        stub = StubLLM([
            LLMResponse(content=None, tool_name="finish", tool_args={"conclusion": "All done."}),
        ])
        agent = MythosAgent(config=make_config(), llm=stub)
        assert agent.run("trivial goal") == "All done."

    def test_tool_call_then_finish(self):
        stub = StubLLM([
            LLMResponse(content="Calculating.", tool_name="calculate", tool_args={"expression": "2 ** 10"}),
            LLMResponse(content=None, tool_name="finish", tool_args={"conclusion": "The answer is 1024."}),
        ])
        agent = MythosAgent(config=make_config(), llm=stub)
        result = agent.run("What is 2 ** 10?")
        assert result == "The answer is 1024."

    def test_plain_text_gets_nudged_then_finishes(self):
        stub = StubLLM([
            LLMResponse(content="Thinking out loud without any tool call."),
            LLMResponse(content=None, tool_name="finish", tool_args={"conclusion": "ok"}),
        ])
        agent = MythosAgent(config=make_config(), llm=stub)
        assert agent.run("goal") == "ok"

    def test_unknown_tool_reported_and_loop_continues(self):
        stub = StubLLM([
            LLMResponse(content=None, tool_name="no_such_tool", tool_args={}),
            LLMResponse(content=None, tool_name="finish", tool_args={"conclusion": "recovered"}),
        ])
        agent = MythosAgent(config=make_config(), llm=stub)
        assert agent.run("goal") == "recovered"

    def test_iteration_cap_stops_runaway_agent(self):
        # Stub that always returns plain text -> the loop must be stopped by the monitor.
        class ChattyStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                return LLMResponse(content="still thinking...")

        agent = MythosAgent(config=make_config(max_iterations=4), llm=ChattyStub())
        result = agent.run("goal that never finishes")
        assert "Maximum iteration limit" in result or "stopped" in result.lower()

    def test_custom_tool_is_callable(self):
        calls = []

        def greet(name: str) -> str:
            calls.append(name)
            return f"Hello, {name}!"

        stub = StubLLM([
            LLMResponse(content=None, tool_name="greet", tool_args={"name": "Ada"}),
            LLMResponse(content=None, tool_name="finish", tool_args={"conclusion": "greeted"}),
        ])
        agent = MythosAgent(config=make_config(), llm=stub)
        agent.add_tool(Tool(
            name="greet",
            description="Greet a person.",
            parameters={"name": {"type": "string"}},
            func=greet,
            required=["name"],
        ))
        assert agent.run("greet Ada") == "greeted"
        assert calls == ["Ada"]

    def test_memory_tools_are_wired(self):
        stub = StubLLM([
            LLMResponse(content=None, tool_name="memory_store", tool_args={"key": "k", "value": "v"}),
            LLMResponse(content=None, tool_name="memory_recall", tool_args={"key": "k"}),
            LLMResponse(content=None, tool_name="finish", tool_args={"conclusion": "done"}),
        ])
        agent = MythosAgent(config=make_config(), llm=stub)
        agent.run("store and recall")
        assert agent._memory.long.get("k") == "v"

    def test_consecutive_llm_errors_stop_the_run(self):
        class FailingLLM(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                raise RuntimeError("simulated API outage")

        agent = MythosAgent(config=make_config(max_consecutive_failures=3), llm=FailingLLM())
        result = agent.run("goal")
        assert "failed" in result.lower() or "stopped" in result.lower()
