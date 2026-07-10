"""
tests/test_guardrails.py
------------------------
Hard boundaries on file/shell tools: protected paths and destructive commands.
"""
import pytest

from mythos import guardrails
from mythos.guardrails import check_path, check_shell
from mythos.tools import _tool_run_shell, _tool_write_file


class TestPathGuard:
    def test_protected_system_path_blocked(self):
        assert check_path("/etc/passwd", write=True) is not None
        assert check_path("/usr/bin/python", write=True) is not None
        assert check_path("/boot/grub/x", write=True) is not None

    def test_reads_always_allowed(self):
        assert check_path("/etc/passwd", write=False) is None

    def test_ordinary_path_allowed(self, tmp_path):
        assert check_path(str(tmp_path / "out.txt"), write=True) is None

    def test_extra_protected_paths_from_env(self, tmp_path, monkeypatch):
        secret = tmp_path / "vault"
        monkeypatch.setenv("MYTHOS_PROTECTED_PATHS", str(secret))
        assert check_path(str(secret / "key"), write=True) is not None

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("MYTHOS_GUARDRAILS", "off")
        assert check_path("/etc/passwd", write=True) is None


class TestShellGuard:
    @pytest.mark.parametrize("cmd", [
        "rm -rf /",
        "rm -rf /*",
        "sudo rm -fr /var",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "shutdown -h now",
        ":(){ :|:& };:",
    ])
    def test_destructive_commands_blocked(self, cmd):
        assert check_shell(cmd) is not None

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "python script.py",
        "rm -rf ./build",          # a relative build dir is fine
        "grep -r pattern .",
    ])
    def test_ordinary_commands_allowed(self, cmd):
        assert check_shell(cmd) is None


class TestToolIntegration:
    def test_write_file_refuses_protected_path(self):
        result = _tool_write_file("/etc/mythos_test", "x")
        assert result.startswith("ERROR:")
        assert "protected" in result

    def test_run_shell_refuses_destructive(self):
        result = _tool_run_shell("rm -rf /")
        assert result.startswith("ERROR:")

    def test_write_file_allows_tmp(self, tmp_path):
        target = tmp_path / "ok.txt"
        assert _tool_write_file(str(target), "hello").startswith("Written")
        assert target.read_text() == "hello"
