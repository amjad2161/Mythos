"""
tests/test_hardening.py
-----------------------
Tests for the security / robustness hardening of the tools and memory layers.
"""
import sys

from mythos.memory import Message, ShortTermMemory
from mythos.tools import (
    _MAX_TOOL_OUTPUT_CHARS,
    _tool_calculate,
    _tool_read_file,
    _tool_run_shell,
    _truncate,
    build_default_registry,
)


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
        # sys.executable (not "python3") so the test also runs on Windows.
        result = _tool_run_shell(f'"{sys.executable}" -c "print(\'x\' * 100000)"')
        assert "truncated" in result
        assert len(result) <= _MAX_TOOL_OUTPUT_CHARS

    def test_invalid_timeout_is_coerced(self):
        # Must not raise on a bad timeout value.
        assert _tool_run_shell("echo ok", timeout="not-an-int").strip() == "ok"


class TestCalculatorResourceLimits:
    def test_huge_exponent_rejected(self):
        assert _tool_calculate("9**9**9").startswith("ERROR:")

    def test_huge_intermediate_rejected(self):
        assert _tool_calculate("(2**512)**512").startswith("ERROR:")

    def test_factorial_argument_capped(self):
        assert _tool_calculate("factorial(100000000)").startswith("ERROR:")

    def test_expression_node_count_capped(self):
        assert _tool_calculate("+".join(["1"] * 500)).startswith("ERROR:")

    def test_expression_length_capped(self):
        assert _tool_calculate("1" * 20_000).startswith("ERROR:")

    def test_normal_math_still_works(self):
        assert _tool_calculate("2 ** 10") == "1024"
        assert _tool_calculate("factorial(10)") == "3628800"
        assert _tool_calculate("(-1) ** 10**9") == "1"  # |base| <= 1 stays legal


class TestOutputCaps:
    def test_truncate_never_exceeds_limit(self):
        out = _truncate("x" * 50_000, 1_000)
        assert len(out) <= 1_000
        assert "truncated" in out

    def test_read_file_capped(self, tmp_path):
        big = tmp_path / "big.txt"
        big.write_text("x" * 300_000, encoding="utf-8")
        out = _tool_read_file(str(big))
        assert len(out) <= _MAX_TOOL_OUTPUT_CHARS
        # Only a prefix of the file is read, so the notice must state the cap
        # rather than claim an exact omitted count it cannot know.
        assert "truncated: file exceeds" in out


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
