"""
tests/test_tools_computer.py
----------------------------
Computer-use tools: graceful degradation when no backend is present, safe
argv construction (never shell), SSRF reuse for open_url, and guardrail
enforcement — all with mocked OS backends (the CI host is headless).
"""
import subprocess

import pytest

from mythos import tools_computer
from mythos.tools_computer import (
    _tool_clipboard_get,
    _tool_clipboard_set,
    _tool_notify,
    _tool_open_path,
    _tool_open_url,
    _tool_screenshot,
)


class TestOpenUrl:
    def test_rejects_non_http_scheme(self):
        assert _tool_open_url("file:///etc/passwd").startswith("ERROR:")

    def test_rejects_private_host(self):
        # SSRF policy reused from tools_web: metadata/loopback blocked.
        assert _tool_open_url("http://169.254.169.254/").startswith("ERROR:")

    def test_opens_valid_url(self, monkeypatch):
        opened = {}
        # _validate_url is imported from tools_web inside the function; patch there.
        monkeypatch.setattr(
            "mythos.tools_web._validate_url", lambda url: (None, object())
        )

        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url) or True)
        result = _tool_open_url("https://example.com")
        assert opened["url"] == "https://example.com"
        assert "Opened" in result


class TestOpenPath:
    def test_missing_path(self, tmp_path):
        assert _tool_open_path(str(tmp_path / "nope.txt")).startswith("ERROR:")

    def test_guardrail_blocks_protected(self, monkeypatch, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        monkeypatch.setattr(
            "mythos.guardrails.check_path", lambda path, write: "protected path"
        )
        assert _tool_open_path(str(f)).startswith("ERROR:")

    def test_opens_with_argv_no_shell(self, monkeypatch, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        monkeypatch.setattr("mythos.guardrails.check_path", lambda path, write: None)
        monkeypatch.setattr(tools_computer, "_openers", lambda: [["xdg-open"]])
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(tools_computer, "_run", fake_run)
        result = _tool_open_path(str(f))
        assert seen["argv"] == ["xdg-open", str(f)]
        assert result == f"Opened {f}"

    def test_no_opener_available(self, monkeypatch, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        monkeypatch.setattr("mythos.guardrails.check_path", lambda path, write: None)
        monkeypatch.setattr(tools_computer, "_openers", lambda: [])
        assert _tool_open_path(str(f)).startswith("ERROR:")


class TestClipboard:
    def test_get_via_backend(self, monkeypatch):
        monkeypatch.setattr(tools_computer, "_clipboard_backends", lambda write: [["xclip"]])

        def fake_run(argv, input_text=None):
            return subprocess.CompletedProcess(argv, 0, "hello world", "")

        monkeypatch.setattr(tools_computer, "_run", fake_run)
        # ensure pyperclip path is skipped
        monkeypatch.setitem(__import__("sys").modules, "pyperclip", None)
        assert _tool_clipboard_get() == "hello world"

    def test_set_via_backend(self, monkeypatch):
        monkeypatch.setattr(tools_computer, "_clipboard_backends", lambda write: [["xclip"]])
        captured = {}

        def fake_run(argv, input_text=None):
            captured["text"] = input_text
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(tools_computer, "_run", fake_run)
        monkeypatch.setitem(__import__("sys").modules, "pyperclip", None)
        assert _tool_clipboard_set("copy me").startswith("Copied")
        assert captured["text"] == "copy me"

    def test_no_backend(self, monkeypatch):
        monkeypatch.setattr(tools_computer, "_clipboard_backends", lambda write: [])
        monkeypatch.setitem(__import__("sys").modules, "pyperclip", None)
        assert _tool_clipboard_get().startswith("ERROR:")

    def test_set_oversized_rejected(self):
        assert _tool_clipboard_set("x" * 200_000).startswith("ERROR:")


class TestNotify:
    def test_no_backend(self, monkeypatch):
        monkeypatch.setattr(tools_computer.platform, "system", lambda: "Linux")
        monkeypatch.setattr(tools_computer.shutil, "which", lambda cmd: None)
        assert _tool_notify("t", "m").startswith("ERROR:")

    def test_linux_notify_send(self, monkeypatch):
        monkeypatch.setattr(tools_computer.platform, "system", lambda: "Linux")
        monkeypatch.setattr(tools_computer.shutil, "which", lambda cmd: "/usr/bin/notify-send")
        seen = {}

        def fake_run(argv, input_text=None):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(tools_computer, "_run", fake_run)
        assert _tool_notify("Title", "Body") == "Notified: Title"
        assert seen["argv"] == ["notify-send", "Title", "Body"]


class TestScreenshot:
    def test_guardrail_blocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "mythos.guardrails.check_path", lambda path, write: "protected path"
        )
        assert _tool_screenshot(str(tmp_path / "s.png")).startswith("ERROR:")

    def test_no_backend(self, monkeypatch, tmp_path):
        monkeypatch.setattr("mythos.guardrails.check_path", lambda path, write: None)
        monkeypatch.setattr(tools_computer.platform, "system", lambda: "Linux")
        monkeypatch.setattr(tools_computer.shutil, "which", lambda cmd: None)
        monkeypatch.setitem(__import__("sys").modules, "mss", None)
        assert _tool_screenshot(str(tmp_path / "s.png")).startswith("ERROR:")

    def test_linux_scrot(self, monkeypatch, tmp_path):
        monkeypatch.setattr("mythos.guardrails.check_path", lambda path, write: None)
        monkeypatch.setattr(tools_computer.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            tools_computer.shutil, "which", lambda cmd: "/usr/bin/scrot" if cmd == "scrot" else None
        )
        monkeypatch.setitem(__import__("sys").modules, "mss", None)
        target = tmp_path / "s.png"

        def fake_run(argv, input_text=None):
            target.write_bytes(b"PNG")  # simulate the tool writing the file
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(tools_computer, "_run", fake_run)
        assert _tool_screenshot(str(target)) == f"Saved screenshot to {target}"


class TestActionVocabulary:
    def _fake_backend(self, monkeypatch):
        calls = []

        class Fake:
            def move(self, x, y): calls.append(("move", x, y))
            def click(self, x, y, button, count): calls.append(("click", x, y, button, count))
            def type(self, text): calls.append(("type", text))
            def key(self, keys): calls.append(("key", keys))
            def scroll(self, direction, amount): calls.append(("scroll", direction, amount))

        from mythos.tools_computer import set_action_backend
        set_action_backend(Fake())
        return calls

    def teardown_method(self):
        from mythos.tools_computer import set_action_backend
        set_action_backend(None)

    def test_move_click_type_key_scroll(self, monkeypatch):
        from mythos.tools_computer import (
            _tool_computer_click, _tool_computer_key, _tool_computer_move,
            _tool_computer_scroll, _tool_computer_type,
        )
        calls = self._fake_backend(monkeypatch)
        assert _tool_computer_move(100, 200) == "Moved cursor to (100, 200)"
        assert "left-clicked at (10, 20)" in _tool_computer_click(10, 20)
        assert _tool_computer_type("hi").startswith("Typed")
        assert _tool_computer_key("ctrl+s") == "Pressed ctrl+s"
        assert _tool_computer_scroll("down", 2) == "Scrolled down ×2"
        assert ("move", 100, 200) in calls
        assert ("click", 10, 20, "left", 1) in calls
        assert ("key", "ctrl+s") in calls

    def test_bad_args(self, monkeypatch):
        from mythos.tools_computer import (
            _tool_computer_click, _tool_computer_key, _tool_computer_scroll,
        )
        self._fake_backend(monkeypatch)
        assert _tool_computer_click(1, 2, button="middleish").startswith("ERROR:")
        assert _tool_computer_key("   ").startswith("ERROR:")
        assert _tool_computer_scroll("sideways").startswith("ERROR:")

    def test_no_backend_degrades(self, monkeypatch):
        from mythos.tools_computer import _tool_computer_click, set_action_backend
        set_action_backend(None)
        monkeypatch.setattr(
            "mythos.tools_computer._get_action_backend", lambda: None
        )
        assert _tool_computer_click(1, 2).startswith("ERROR: no input backend")


class TestRegistrationAndRoles:
    def test_computer_and_assistant_tools_registered(self):
        from mythos.tools import build_default_registry

        names = set(build_default_registry().names())
        assert {"open_url", "clipboard_get", "screenshot"} <= names
        assert {"pa_add_task", "pa_daily_brief"} <= names

    def test_roles_exist_and_operator_has_no_shell(self):
        from mythos.orchestration.roles import ROLE_TOOLS, build_registry_for_role

        assert "assistant" in ROLE_TOOLS
        assert "operator" in ROLE_TOOLS
        assert "run_shell" not in ROLE_TOOLS["operator"]
        assert "run_shell" not in ROLE_TOOLS["assistant"]
        # both roles build a valid registry
        assert build_registry_for_role("operator").get("screenshot") is not None
        assert build_registry_for_role("assistant").get("pa_add_task") is not None

    def test_restricted_operator_is_perception_only(self):
        from mythos.orchestration.roles import build_registry_for_role

        reg = build_registry_for_role("operator", access_level="restricted")
        names = set(reg.names())
        # perception survives; outward actions are stripped
        assert "screenshot" in names
        assert "clipboard_get" in names
        assert "open_url" not in names
        assert "clipboard_set" not in names
        assert "notify" not in names
