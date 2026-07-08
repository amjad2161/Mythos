"""
tests/test_llm_wire.py
----------------------
Tests for the provider-neutral -> wire-format message translation.

These exercise the translation helpers directly (no network, no SDK), which is
where the historically broken tool-calling history was produced.
"""
import json

from mythos.llm import _to_anthropic_messages, _to_openai_messages


def _history():
    """A realistic one-tool-call conversation in neutral form."""
    return [
        {"role": "system", "content": "You are Mythos."},
        {"role": "user", "content": "Goal: compute 2**10"},
        {
            "role": "assistant",
            "content": "I'll calculate that.",
            "tool_name": "calculate",
            "tool_args": {"expression": "2 ** 10"},
            "tool_call_id": "call_1",
        },
        {"role": "tool", "content": "1024", "name": "calculate", "tool_call_id": "call_1"},
    ]


class TestAnthropicTranslation:
    def test_system_is_hoisted_out_of_messages(self):
        system, msgs = _to_anthropic_messages(_history())
        assert system == "You are Mythos."
        assert all(m["role"] in ("user", "assistant") for m in msgs)

    def test_assistant_tool_call_becomes_tool_use_block(self):
        _, msgs = _to_anthropic_messages(_history())
        assistant = next(m for m in msgs if m["role"] == "assistant")
        types = [b["type"] for b in assistant["content"]]
        assert "tool_use" in types
        # Text emitted alongside the tool call is preserved.
        assert "text" in types
        tool_use = next(b for b in assistant["content"] if b["type"] == "tool_use")
        assert tool_use["id"] == "call_1"
        assert tool_use["name"] == "calculate"
        assert tool_use["input"] == {"expression": "2 ** 10"}

    def test_tool_result_becomes_user_tool_result_block(self):
        _, msgs = _to_anthropic_messages(_history())
        # The tool result must be a user turn carrying a tool_result block that
        # references the originating tool_use id.
        result_turn = msgs[-1]
        assert result_turn["role"] == "user"
        block = result_turn["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "call_1"
        assert block["content"] == "1024"

    def test_no_raw_tool_role_leaks_through(self):
        _, msgs = _to_anthropic_messages(_history())
        assert all(m["role"] != "tool" for m in msgs)


class TestOpenAITranslation:
    def test_assistant_tool_call_has_tool_calls_array(self):
        msgs = _to_openai_messages(_history())
        assistant = next(m for m in msgs if m["role"] == "assistant" and m.get("tool_calls"))
        tc = assistant["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "calculate"
        assert json.loads(tc["function"]["arguments"]) == {"expression": "2 ** 10"}

    def test_tool_result_has_tool_call_id(self):
        msgs = _to_openai_messages(_history())
        tool_turn = next(m for m in msgs if m["role"] == "tool")
        assert tool_turn["tool_call_id"] == "call_1"
        assert tool_turn["content"] == "1024"

    def test_plain_turns_pass_through(self):
        msgs = _to_openai_messages(_history())
        assert msgs[0] == {"role": "system", "content": "You are Mythos."}
        assert msgs[1] == {"role": "user", "content": "Goal: compute 2**10"}
