"""
tests/test_tools_browser.py
---------------------------
Browser web-use tools: SSRF reuse, graceful web_fetch fallback when Playwright
is absent, indexed-element reads via an injected backend, guardrails, and role
registration. No real browser is launched.
"""
import pytest

from mythos import tools_browser
from mythos.tools_browser import (
    _FallbackBackend,
    _tool_browser_click,
    _tool_browser_fill,
    _tool_browser_navigate,
    _tool_browser_read_page,
    _tool_browser_screenshot,
    set_backend,
)


@pytest.fixture(autouse=True)
def _reset_backend():
    set_backend(None)
    yield
    set_backend(None)


class FakeBackend:
    def __init__(self):
        self.calls = []

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return f"Navigated to {url}"

    def read(self, mode):
        self.calls.append(("read", mode))
        return "[0] <a> Home -> /"

    def click(self, target):
        self.calls.append(("click", target))
        return f"Clicked {target}"

    def fill(self, target, value):
        self.calls.append(("fill", target, value))
        return f"Filled {target}"

    def screenshot(self, path):
        self.calls.append(("screenshot", path))
        return f"Saved screenshot to {path}"

    def close(self):
        pass


class TestSSRF:
    def test_navigate_rejects_non_http(self):
        assert _tool_browser_navigate("file:///etc/passwd").startswith("ERROR:")

    def test_navigate_rejects_private_host(self):
        assert _tool_browser_navigate("http://169.254.169.254/").startswith("ERROR:")


class TestWithInjectedBackend:
    def test_navigate_read_click_fill(self):
        fake = FakeBackend()
        set_backend(fake)
        assert _tool_browser_navigate("https://example.com") == "Navigated to https://example.com"
        assert "[0]" in _tool_browser_read_page("interactive")
        assert _tool_browser_click("0") == "Clicked 0"
        assert _tool_browser_fill("#q", "hello") == "Filled #q"
        assert ("fill", "#q", "hello") in fake.calls

    def test_read_page_bad_mode(self):
        set_backend(FakeBackend())
        assert _tool_browser_read_page("pixels").startswith("ERROR:")

    def test_screenshot_guardrail(self, monkeypatch, tmp_path):
        set_backend(FakeBackend())
        monkeypatch.setattr("mythos.guardrails.check_path", lambda path, write: "protected")
        assert _tool_browser_screenshot(str(tmp_path / "s.png")).startswith("ERROR:")

    def test_screenshot_ok(self, monkeypatch, tmp_path):
        set_backend(FakeBackend())
        monkeypatch.setattr("mythos.guardrails.check_path", lambda path, write: None)
        assert _tool_browser_screenshot(str(tmp_path / "s.png")).startswith("Saved screenshot")


class TestFallbackBackend:
    def test_navigate_then_read_uses_web_fetch(self, monkeypatch):
        monkeypatch.setattr(
            "mythos.tools_web._tool_web_fetch", lambda url, max_bytes=None: f"BODY of {url}"
        )
        be = _FallbackBackend()
        assert "no-JS fallback" in be.navigate("https://example.com")
        assert be.read("text") == "BODY of https://example.com"

    def test_read_before_navigate(self):
        assert _FallbackBackend().read("text").startswith("ERROR:")

    def test_interaction_unavailable(self):
        be = _FallbackBackend()
        assert be.click("0").startswith("ERROR:")
        assert be.fill("#q", "x").startswith("ERROR:")
        assert be.screenshot("s.png").startswith("ERROR:")

    def test_get_backend_falls_back_without_playwright(self, monkeypatch):
        # Force the Playwright backend constructor to fail → fallback is used.
        monkeypatch.setattr(
            tools_browser, "_PlaywrightBackend",
            lambda: (_ for _ in ()).throw(ImportError("no playwright")),
        )
        set_backend(None)
        backend = tools_browser._get_backend()
        assert isinstance(backend, _FallbackBackend)


class TestRoleRegistration:
    def test_browser_tools_registered(self):
        from mythos.tools import build_default_registry

        names = set(build_default_registry().names())
        assert {"browser_navigate", "browser_read_page", "browser_click"} <= names

    def test_browser_role_no_shell_and_restricted_is_read_only(self):
        from mythos.orchestration.roles import ROLE_TOOLS, build_registry_for_role

        assert "browser" in ROLE_TOOLS
        assert "run_shell" not in ROLE_TOOLS["browser"]
        restricted = set(build_registry_for_role("browser", access_level="restricted").names())
        assert "browser_read_page" in restricted     # perception survives
        assert "browser_navigate" not in restricted   # outward action stripped
        assert "browser_click" not in restricted
