"""
mythos/tools_browser.py
-----------------------
Web-use tools for the ``browser`` role.

Real browser automation over Playwright when it is available, degrading
gracefully to the SSRF-hardened, read-only ``web_fetch`` when it is not — so
the role always offers *some* web capability offline (JavaScript-free reads)
and full navigate/click/fill/screenshot when a browser is present.

Perception follows the browser-use / Playwright-MCP pattern: ``browser_read_page``
returns a compact **indexed list of interactive elements** (not raw HTML or
pixels), and actions address elements by that index or a CSS selector — cheaper
and more reliable than screenshot-clicking.

Safety:
* Every ``browser_navigate`` URL passes the same SSRF policy as ``web_fetch``
  (scheme allowlist, private/loopback/metadata blocking) *before* the browser
  opens it.
* Navigation/interaction are outward actions — with ``MYTHOS_APPROVALS=on`` they
  pass the human-in-the-loop gate like any other tool call.

Config: ``MYTHOS_BROWSER_PATH`` overrides the Chromium executable path;
otherwise Playwright's configured browser is used.  All tools return
``"ERROR: ..."`` strings on failure and never raise.
"""
from __future__ import annotations

import os
from typing import List, Optional

from .tools import Tool, _truncate

_MAX_ELEMENTS = 60
_MAX_TEXT = 8000
_INTERACTIVE = "a, button, input, textarea, select, [role=button], [role=link]"


class _Backend:
    """Interface every browser backend implements."""

    def navigate(self, url: str) -> str: ...
    def read(self, mode: str) -> str: ...
    def click(self, target: str) -> str: ...
    def fill(self, target: str, value: str) -> str: ...
    def screenshot(self, path: str) -> str: ...
    def close(self) -> None: ...


class _FallbackBackend(_Backend):
    """No-browser mode: read-only reads via web_fetch; interaction unavailable."""

    def __init__(self) -> None:
        self._url = ""

    def navigate(self, url: str) -> str:
        self._url = url
        return f"Fetched {url} (no-JS fallback — install Playwright for full browsing)"

    def read(self, mode: str) -> str:
        if not self._url:
            return "ERROR: navigate to a URL first"
        from .tools_web import _tool_web_fetch  # noqa: PLC0415

        body = _tool_web_fetch(self._url)
        return _truncate(body, _MAX_TEXT)

    def _no_browser(self, action: str) -> str:
        return (
            f"ERROR: {action} needs a real browser — install Playwright "
            "(`pip install playwright && playwright install chromium`)"
        )

    def click(self, target: str) -> str:
        return self._no_browser("click")

    def fill(self, target: str, value: str) -> str:
        return self._no_browser("fill")

    def screenshot(self, path: str) -> str:
        return self._no_browser("screenshot")

    def close(self) -> None:
        self._url = ""


class _PlaywrightBackend(_Backend):
    """Full browsing over a persistent Playwright Chromium context."""

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        self._pw = sync_playwright().start()
        launch_kwargs = {}
        exe = os.getenv("MYTHOS_BROWSER_PATH")
        if exe:
            launch_kwargs["executable_path"] = exe
        self._browser = self._pw.chromium.launch(headless=True, **launch_kwargs)
        self._page = self._browser.new_page()

    def navigate(self, url: str) -> str:
        response = self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        status = response.status if response else "?"
        return f"Navigated to {self._page.url} (HTTP {status}) — {self._page.title()!r}"

    def read(self, mode: str) -> str:
        if mode == "text":
            return _truncate(self._page.inner_text("body"), _MAX_TEXT)
        elements = self._page.query_selector_all(_INTERACTIVE)
        lines: List[str] = [f"URL: {self._page.url}", f"Title: {self._page.title()}", "Interactive:"]
        for i, el in enumerate(elements[:_MAX_ELEMENTS]):
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            label = (el.inner_text() or el.get_attribute("aria-label") or
                     el.get_attribute("value") or el.get_attribute("placeholder") or "").strip()
            href = el.get_attribute("href") or ""
            lines.append(f"[{i}] <{tag}> {label[:80]}" + (f"  -> {href}" if href else ""))
        return _truncate("\n".join(lines), _MAX_TEXT)

    def _resolve(self, target: str):
        if target.isdigit():
            elements = self._page.query_selector_all(_INTERACTIVE)
            idx = int(target)
            if idx >= len(elements):
                return None
            return elements[idx]
        return self._page.query_selector(target)

    def click(self, target: str) -> str:
        el = self._resolve(target)
        if el is None:
            return f"ERROR: no element for target {target!r}"
        el.click(timeout=10000)
        return f"Clicked {target}; now at {self._page.url}"

    def fill(self, target: str, value: str) -> str:
        el = self._resolve(target)
        if el is None:
            return f"ERROR: no element for target {target!r}"
        el.fill(value, timeout=10000)
        return f"Filled {target}"

    def screenshot(self, path: str) -> str:
        self._page.screenshot(path=path, full_page=True)
        return f"Saved screenshot to {path}"

    def close(self) -> None:
        try:
            self._browser.close()
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


_BACKEND: Optional[_Backend] = None


def _get_backend() -> _Backend:
    """Return the active backend, creating a Playwright one if possible."""
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    try:
        _BACKEND = _PlaywrightBackend()
    except Exception:  # noqa: BLE001 – Playwright missing or launch failed
        _BACKEND = _FallbackBackend()
    return _BACKEND


def set_backend(backend: Optional[_Backend]) -> None:
    """Inject a backend (tests) or reset to lazy creation with None."""
    global _BACKEND
    _BACKEND = backend


def _tool_browser_navigate(url: str) -> str:
    from .tools_web import _validate_url  # noqa: PLC0415

    reason, _ = _validate_url(url)
    if reason:
        return f"ERROR: refusing to navigate to {url!r}: {reason}"
    try:
        return _get_backend().navigate(url)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: navigation failed: {exc}"


def _tool_browser_read_page(mode: str = "interactive") -> str:
    if mode not in ("interactive", "text"):
        return "ERROR: mode must be 'interactive' or 'text'"
    try:
        return _get_backend().read(mode)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: read failed: {exc}"


def _tool_browser_click(target: str) -> str:
    try:
        return _get_backend().click(target)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: click failed: {exc}"


def _tool_browser_fill(target: str, value: str) -> str:
    try:
        return _get_backend().fill(target, value)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: fill failed: {exc}"


def _tool_browser_screenshot(output_path: str) -> str:
    from .guardrails import check_path  # noqa: PLC0415

    blocked = check_path(output_path, write=True)
    if blocked:
        return f"ERROR: {blocked}"
    try:
        return _get_backend().screenshot(output_path)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: screenshot failed: {exc}"


BROWSER_TOOLS: List[Tool] = [
    Tool(
        name="browser_navigate",
        description="Open a URL in the browser (http/https; SSRF-checked).",
        parameters={"url": {"type": "string", "description": "The URL to open."}},
        func=_tool_browser_navigate,
        required=["url"],
    ),
    Tool(
        name="browser_read_page",
        description=(
            "Read the current page. mode='interactive' returns an indexed list "
            "of clickable/typable elements; mode='text' returns readable text."
        ),
        parameters={
            "mode": {"type": "string", "description": "interactive | text.", "default": "interactive"},
        },
        func=_tool_browser_read_page,
        required=[],
    ),
    Tool(
        name="browser_click",
        description="Click an element by its read_page index or a CSS selector.",
        parameters={"target": {"type": "string", "description": "Element index or CSS selector."}},
        func=_tool_browser_click,
        required=["target"],
    ),
    Tool(
        name="browser_fill",
        description="Type a value into an input by index or CSS selector.",
        parameters={
            "target": {"type": "string", "description": "Element index or CSS selector."},
            "value": {"type": "string", "description": "Text to enter."},
        },
        func=_tool_browser_fill,
        required=["target", "value"],
    ),
    Tool(
        name="browser_screenshot",
        description="Capture the current page to a PNG file.",
        parameters={"output_path": {"type": "string", "description": "Where to write the PNG."}},
        func=_tool_browser_screenshot,
        required=["output_path"],
    ),
]
