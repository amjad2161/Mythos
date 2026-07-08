"""
tests/test_hardening.py
-----------------------
Tests for the security / robustness hardening of the tools and memory layers.
"""
from mythos.memory import Message, ShortTermMemory
from mythos.tools import _tool_calculate, _tool_run_shell, build_default_registry


class TestCalculatorSandbox:
    def test_arithmetic_still_works(self):
        assert _tool_calculate("2 + 2") == "4"
        assert _tool_calculate("2 ** 10") == "1024"
        assert _tool_calculate("sqrt(16)") == "4.0"
        assert _tool_calculate("max(3, 7)") == "7"

    def test_pi_constant(self):
        assert _tool_calculate("pi").startswith("3.14159")

    def test_blocks_import_escape(self):
        assert _tool_calculate("__import__('os').system('echo bad')").startswith("ERROR:")

    def test_blocks_attribute_access_escape(self):
        # The classic empty-__builtins__ escape via attribute access.
        assert _tool_calculate("(1).__class__").startswith("ERROR:")
        assert _tool_calculate("().__class__.__bases__").startswith("ERROR:")

    def test_blocks_unknown_names(self):
        assert _tool_calculate("open('/etc/passwd')").startswith("ERROR:")


class TestShellHardening:
    def test_nonzero_exit_is_reported(self):
        result = _tool_run_shell("exit 3")
        assert "exit code 3" in result

    def test_output_is_capped(self):
        result = _tool_run_shell("python3 -c \"print('x' * 100000)\"")
        assert "truncated" in result
        assert len(result) < 30_000

    def test_invalid_timeout_is_coerced(self):
        # Must not raise on a bad timeout value.
        assert _tool_run_shell("echo ok", timeout="not-an-int").strip() == "ok"


class TestRegistryArgGuard:
    def test_non_dict_arguments_do_not_crash(self):
        registry = build_default_registry()
        result = registry.call("calculate", ["not", "a", "dict"])
        assert result.startswith("ERROR:")


class TestEvictionKeepsToolPairsValid:
    def test_orphan_tool_result_dropped_after_eviction(self):
        stm = ShortTermMemory(window=2)
        stm.add(Message(role="system", content="sys"))
        # An assistant tool call + its result, then two more turns to force the
        # pair out of the window.
        stm.add(Message(role="assistant", content="", tool_name="t",
                        tool_args={}, tool_call_id="c1"))
        stm.add(Message(role="tool", content="res", name="t", tool_call_id="c1"))
        stm.add(Message(role="user", content="next"))
        stm.add(Message(role="user", content="another"))
        roles = [m.role for m in stm.get_all()]
        # System survives; the first non-system message is never a bare tool
        # result (which the Anthropic API would reject).
        assert roles[0] == "system"
        first_non_system = next(r for r in roles if r != "system")
        assert first_non_system != "tool"
