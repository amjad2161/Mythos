"""
tests/test_llm.py
-----------------
Unit tests for the LLM provider abstraction.
"""
import pytest
from mythos.llm import (
    LLMResponse,
    StubLLM,
    create_llm,
    AnthropicLLM,
    OpenAILLM,
)


class TestLLMResponse:
    def test_no_tool_call(self):
        r = LLMResponse(content="hello")
        assert not r.has_tool_call
        assert r.content == "hello"

    def test_with_tool_call(self):
        r = LLMResponse(content=None, tool_name="finish", tool_args={"conclusion": "done"})
        assert r.has_tool_call
        assert r.tool_name == "finish"
        assert r.tool_args == {"conclusion": "done"}

    def test_default_tool_args_empty_dict(self):
        r = LLMResponse(content="x")
        assert r.tool_args == {}

    def test_repr_no_tool(self):
        r = LLMResponse(content="hello world")
        assert "hello world" in repr(r)

    def test_repr_with_tool(self):
        r = LLMResponse(content=None, tool_name="my_tool", tool_args={"k": "v"})
        assert "my_tool" in repr(r)


class TestStubLLM:
    def test_returns_scripted_responses_in_order(self):
        r1 = LLMResponse(content="first")
        r2 = LLMResponse(content="second")
        stub = StubLLM(responses=[r1, r2])
        assert stub.chat([]).content == "first"
        assert stub.chat([]).content == "second"

    def test_fallback_finish_when_exhausted(self):
        stub = StubLLM(responses=[])
        resp = stub.chat([])
        assert resp.has_tool_call
        assert resp.tool_name == "finish"

    def test_add_response(self):
        stub = StubLLM()
        stub.add_response(LLMResponse(content="added"))
        assert stub.chat([]).content == "added"

    def test_stub_ignores_tools_param(self):
        stub = StubLLM(responses=[LLMResponse(content="ok")])
        resp = stub.chat([], tools=[{"type": "function", "function": {"name": "x"}}])
        assert resp.content == "ok"


class TestCreateLLMFactory:
    def test_stub_provider_returns_stub(self):
        llm = create_llm("stub", "any-model", None)
        assert isinstance(llm, StubLLM)

    def test_anthropic_alias_requires_package(self):
        # If anthropic is installed, should return AnthropicLLM; otherwise ImportError.
        try:
            import anthropic  # noqa: F401
            llm = create_llm("anthropic", "claude-opus-4-5", "dummy-key")
            assert isinstance(llm, AnthropicLLM)
        except ImportError:
            pytest.skip("anthropic package not installed")

    def test_claude_alias_same_as_anthropic(self):
        try:
            import anthropic  # noqa: F401
            llm = create_llm("claude", "claude-opus-4-5", "dummy-key")
            assert isinstance(llm, AnthropicLLM)
        except ImportError:
            pytest.skip("anthropic package not installed")

    def test_openai_provider_requires_package(self):
        try:
            import openai  # noqa: F401
            llm = create_llm("openai", "gpt-4o", "dummy-key")
            assert isinstance(llm, OpenAILLM)
        except ImportError:
            pytest.skip("openai package not installed")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_llm("unknown_provider", "model", None)

    def test_default_config_provider_resolves_to_anthropic(self):
        """The default MythosConfig points at the Anthropic backend."""
        from mythos.config import MythosConfig
        config = MythosConfig()
        assert config.llm_provider == "anthropic"
        # Factory must accept this without error when package is present
        try:
            import anthropic  # noqa: F401
            llm = create_llm(config.llm_provider, config.llm_model, "dummy-key")
            assert isinstance(llm, AnthropicLLM)
        except ImportError:
            pytest.skip("anthropic package not installed")
