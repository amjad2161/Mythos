"""
mythos/tools_computer.py
------------------------
Computer-use / OS-control seam for the ``operator`` role.

A thin, backend-pluggable interface over the desktop: open URLs and files,
read/write the clipboard, raise a desktop notification, and capture a
screenshot.  The intelligence lives in the model; these tools stay small,
deterministic, and side-effect-explicit — the canonical perception→action
shape from Anthropic computer-use / OpenAI Operator, kept dependency-light.

Every backend degrades gracefully: when no supported mechanism is present
(headless container, missing helper) the tool returns a structured
``"ERROR: ..."`` string instead of raising, exactly like the other domain
tool modules.  Subprocess calls always pass an argv list (never ``shell=True``)
so nothing here can be turned into shell injection.

Safety: the outward/mutating tools here (``open_url``, ``open_path``,
``clipboard_set``, ``notify``) are registered in
``roles._MUTATING_TOOLS``, so a ``restricted`` operator is perception-only
(``screenshot`` + ``clipboard_get``).  See ``docs/JARVIS_BLUEPRINT.md`` §5 for
the human-in-the-loop gate that irreversible/outward actions pass through.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from typing import List, Optional, Sequence

from .tools import Tool

_TIMEOUT_S = 15
_MAX_TEXT = 100_000


def _run(argv: Sequence[str], *, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run *argv* with no shell, capped, capturing output."""
    return subprocess.run(  # noqa: S603 – argv list, never shell=True
        list(argv),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )


def _openers() -> List[List[str]]:
    """Per-platform 'open this thing' command prefixes, most-specific first."""
    system = platform.system()
    if system == "Darwin":
        return [["open"]]
    if system == "Windows":
        return [["cmd", "/c", "start", ""]]
    # Linux / BSD
    prefixes = []
    for cmd in ("xdg-open", "gio", "gnome-open"):
        if shutil.which(cmd):
            prefixes.append([cmd, "open"] if cmd == "gio" else [cmd])
    return prefixes


# --------------------------------------------------------------------------- #
# Launch                                                                       #
# --------------------------------------------------------------------------- #
def _tool_open_url(url: str) -> str:
    """Open *url* in the user's default browser (http/https only)."""
    from .tools_web import _validate_url  # noqa: PLC0415 – reuse scheme/host policy

    reason, _ = _validate_url(url)
    if reason:
        return f"ERROR: refusing to open {url!r}: {reason}"
    try:
        import webbrowser  # noqa: PLC0415

        if webbrowser.open(url):
            return f"Opened {url} in the default browser"
    except Exception as exc:  # noqa: BLE001 – webbrowser is best-effort
        return f"ERROR: could not open URL: {exc}"
    return "ERROR: no browser available to open the URL"


def _tool_open_path(path: str) -> str:
    """Open a local file/folder with the OS default application."""
    from .guardrails import check_path  # noqa: PLC0415

    if not os.path.exists(path):
        return f"ERROR: path does not exist: {path}"
    blocked = check_path(path, write=False)
    if blocked:
        return f"ERROR: {blocked}"
    openers = _openers()
    if not openers:
        return "ERROR: no OS 'open' command available (install xdg-utils on Linux)"
    prefix = openers[0]
    argv = [a for a in prefix if a != ""] + [path]
    try:
        result = _run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ERROR: open failed: {exc}"
    if result.returncode != 0:
        return f"ERROR: open exited {result.returncode}: {(result.stderr or '').strip()}"
    return f"Opened {path}"


# --------------------------------------------------------------------------- #
# Clipboard                                                                    #
# --------------------------------------------------------------------------- #
def _clipboard_backends(write: bool) -> List[List[str]]:
    system = platform.system()
    if system == "Darwin":
        return [["pbcopy"]] if write else [["pbpaste"]]
    if system == "Windows":
        return [["clip"]] if write else [["powershell", "-command", "Get-Clipboard"]]
    out = []
    if shutil.which("wl-copy") or shutil.which("wl-paste"):
        out.append(["wl-copy"] if write else ["wl-paste", "-n"])
    if shutil.which("xclip"):
        out.append(["xclip", "-selection", "clipboard"]
                    + ([] if write else ["-o"]))
    if shutil.which("xsel"):
        out.append(["xsel", "--clipboard", "--input" if write else "--output"])
    return out


def _tool_clipboard_get() -> str:
    """Read the system clipboard text."""
    try:
        import pyperclip  # noqa: PLC0415

        return pyperclip.paste() or "(clipboard is empty)"
    except Exception:  # noqa: BLE001 – fall through to CLI backends
        pass
    for argv in _clipboard_backends(write=False):
        try:
            result = _run(argv)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return result.stdout or "(clipboard is empty)"
    return "ERROR: no clipboard backend available (install xclip/xsel or pyperclip)"


def _tool_clipboard_set(text: str) -> str:
    """Write *text* to the system clipboard."""
    if len(text) > _MAX_TEXT:
        return f"ERROR: text exceeds the {_MAX_TEXT} character cap"
    try:
        import pyperclip  # noqa: PLC0415

        pyperclip.copy(text)
        return f"Copied {len(text)} characters to the clipboard"
    except Exception:  # noqa: BLE001 – fall through to CLI backends
        pass
    for argv in _clipboard_backends(write=True):
        try:
            result = _run(argv, input_text=text)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return f"Copied {len(text)} characters to the clipboard"
    return "ERROR: no clipboard backend available (install xclip/xsel or pyperclip)"


# --------------------------------------------------------------------------- #
# Notify                                                                       #
# --------------------------------------------------------------------------- #
def _tool_notify(title: str, message: str) -> str:
    """Raise a desktop notification."""
    system = platform.system()
    argv: Optional[List[str]] = None
    if system == "Darwin":
        script = f'display notification {message!r} with title {title!r}'
        argv = ["osascript", "-e", script]
    elif system == "Linux" and shutil.which("notify-send"):
        argv = ["notify-send", title, message]
    if argv is None:
        return "ERROR: no desktop notification backend available (install libnotify/notify-send)"
    try:
        result = _run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ERROR: notification failed: {exc}"
    if result.returncode != 0:
        return f"ERROR: notify exited {result.returncode}: {(result.stderr or '').strip()}"
    return f"Notified: {title}"


# --------------------------------------------------------------------------- #
# Screenshot (perception)                                                      #
# --------------------------------------------------------------------------- #
def _tool_screenshot(output_path: str) -> str:
    """Capture the screen to *output_path* (PNG)."""
    from .guardrails import check_path  # noqa: PLC0415

    blocked = check_path(output_path, write=True)
    if blocked:
        return f"ERROR: {blocked}"
    directory = os.path.dirname(os.path.abspath(output_path))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return f"ERROR: could not create output directory: {exc}"

    # Preferred: mss (tiny, cross-platform, pure-python-ish).
    try:
        import mss  # noqa: PLC0415

        with mss.mss() as sct:
            sct.shot(output=output_path)
        if os.path.exists(output_path):
            return f"Saved screenshot to {output_path}"
    except Exception:  # noqa: BLE001 – fall through to CLI tools
        pass

    system = platform.system()
    argv: Optional[List[str]] = None
    if system == "Darwin":
        argv = ["screencapture", "-x", output_path]
    elif system == "Linux":
        for cmd, build in (
            ("grim", lambda: ["grim", output_path]),
            ("scrot", lambda: ["scrot", "-o", output_path]),
            ("import", lambda: ["import", "-window", "root", output_path]),
        ):
            if shutil.which(cmd):
                argv = build()
                break
    if argv is None:
        return "ERROR: no screenshot backend available (install mss, or grim/scrot/screencapture)"
    try:
        result = _run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ERROR: screenshot failed: {exc}"
    if result.returncode != 0 or not os.path.exists(output_path):
        return f"ERROR: screenshot failed: {(result.stderr or '').strip() or 'no output written'}"
    return f"Saved screenshot to {output_path}"


COMPUTER_TOOLS: List[Tool] = [
    Tool(
        name="open_url",
        description="Open a URL (http/https) in the user's default web browser.",
        parameters={"url": {"type": "string", "description": "The URL to open."}},
        func=_tool_open_url,
        required=["url"],
    ),
    Tool(
        name="open_path",
        description="Open a local file or folder with its default application.",
        parameters={"path": {"type": "string", "description": "Path to open."}},
        func=_tool_open_path,
        required=["path"],
    ),
    Tool(
        name="clipboard_get",
        description="Read the current text contents of the system clipboard.",
        parameters={},
        func=_tool_clipboard_get,
        required=[],
    ),
    Tool(
        name="clipboard_set",
        description="Replace the system clipboard contents with the given text.",
        parameters={"text": {"type": "string", "description": "Text to copy."}},
        func=_tool_clipboard_set,
        required=["text"],
    ),
    Tool(
        name="notify",
        description="Show a desktop notification with a title and message.",
        parameters={
            "title": {"type": "string", "description": "Notification title."},
            "message": {"type": "string", "description": "Notification body."},
        },
        func=_tool_notify,
        required=["title", "message"],
    ),
    Tool(
        name="screenshot",
        description="Capture the current screen to a PNG file (perception).",
        parameters={
            "output_path": {"type": "string", "description": "Where to write the PNG."},
        },
        func=_tool_screenshot,
        required=["output_path"],
    ),
]
