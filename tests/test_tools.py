"""
tests/test_tools.py
-------------------
Unit tests for the tools module.
"""
import math
import os
import pytest
from mythos.tools import (
    Tool,
    ToolRegistry,
    build_default_registry,
    _tool_calculate,
    _tool_current_time,
    _tool_list_directory,
    _tool_read_file,
    _tool_write_file,
    _tool_append_file,
)


class TestTool:
    def test_call_success(self):
        t = Tool(
            name="add",
            description="add two numbers",
            parameters={},
            func=lambda x, y: x + y,
            required=["x", "y"],
        )
        assert t.call(x=1, y=2) == "3"

    def test_call_exception_returns_error_string(self):
        def bad():
            raise ValueError("boom")

        t = Tool(name="bad", description="", parameters={}, func=bad)
        result = t.call()
        assert result.startswith("ERROR:")

    def test_to_openai_spec(self):
        t = Tool(
            name="greet",
            description="say hello",
            parameters={"name": {"type": "string"}},
            func=lambda name: f"Hello {name}",
            required=["name"],
        )
        spec = t.to_openai_spec()
        assert spec["type"] == "function"
        assert spec["function"]["name"] == "greet"
        assert "name" in spec["function"]["parameters"]["properties"]


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        t = Tool(name="foo", description="", parameters={}, func=lambda: "bar")
        registry.register(t)
        assert registry.get("foo") is t

    def test_call_known_tool(self):
        registry = ToolRegistry()
        registry.register(Tool(name="echo", description="", parameters={}, func=lambda msg: msg))
        assert registry.call("echo", {"msg": "hello"}) == "hello"

    def test_call_unknown_tool(self):
        registry = ToolRegistry()
        result = registry.call("no_such_tool", {})
        assert "ERROR" in result

    def test_names(self):
        registry = ToolRegistry()
        registry.register(Tool(name="a", description="", parameters={}, func=lambda: ""))
        registry.register(Tool(name="b", description="", parameters={}, func=lambda: ""))
        assert set(registry.names()) == {"a", "b"}

    def test_openai_specs_returns_list(self):
        registry = build_default_registry()
        specs = registry.openai_specs()
        assert isinstance(specs, list)
        assert len(specs) > 0
        assert all(s["type"] == "function" for s in specs)


class TestBuiltinTools:
    def test_current_time_format(self):
        result = _tool_current_time()
        assert "UTC" in result
        assert len(result) > 10

    def test_calculate_basic(self):
        assert _tool_calculate("2 + 2") == "4"
        assert _tool_calculate("sqrt(16)") == "4.0"
        assert _tool_calculate("2 ** 10") == "1024"

    def test_calculate_invalid_raises_error(self):
        result = _tool_calculate("__import__('os').system('echo bad')")
        assert result.startswith("ERROR:")

    def test_read_write_file(self, tmp_path):
        path = str(tmp_path / "test.txt")
        write_result = _tool_write_file(path, "hello world")
        assert "Written" in write_result

        read_result = _tool_read_file(path)
        assert read_result == "hello world"

    def test_read_nonexistent_file(self):
        result = _tool_read_file("/nonexistent/path/file.txt")
        assert result.startswith("ERROR:")

    def test_append_file(self, tmp_path):
        path = str(tmp_path / "append.txt")
        _tool_write_file(path, "line1\n")
        _tool_append_file(path, "line2\n")
        content = _tool_read_file(path)
        assert "line1" in content
        assert "line2" in content

    def test_list_directory(self, tmp_path):
        (tmp_path / "file.txt").write_text("x")
        (tmp_path / "subdir").mkdir()
        result = _tool_list_directory(str(tmp_path))
        assert "file.txt" in result
        assert "subdir" in result

    def test_list_directory_default(self):
        result = _tool_list_directory(".")
        # Should not raise; content varies by environment
        assert isinstance(result, str)

    def test_default_registry_has_finish(self):
        registry = build_default_registry()
        assert registry.get("finish") is not None

    def test_default_registry_has_memory_tools(self):
        registry = build_default_registry()
        assert registry.get("memory_store") is not None
        assert registry.get("memory_recall") is not None
        assert registry.get("memory_list") is not None
