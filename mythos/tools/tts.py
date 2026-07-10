"""
mythos/tools_tts.py
-------------------
Text-to-speech tool for the voice agent role.

Talks to any OpenAI-compatible speech endpoint (``POST /v1/audio/speech``) —
the reference sidecar is supertonic (``supertonic serve``, MIT-licensed code;
note its model weights ship under OpenRAIL-M).  Configuration:

* ``MYTHOS_TTS_URL``   – base URL of the sidecar (e.g. ``http://localhost:8000``).
  Unset → the tool returns a structured ERROR explaining how to start one.
* ``MYTHOS_TTS_MODEL`` – model name sent to the endpoint (default ``supertonic``).

Every failure path returns an ``"ERROR: ..."`` string; the audio response is
size-capped and written to the path the agent names.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import List

from . import Tool

_TIMEOUT_S = 60
_MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB


def _tool_speak(text: str, output_path: str, voice: str = "default") -> str:
    """Synthesize *text* to a WAV/MP3 file at *output_path* via the TTS sidecar."""
    base = os.getenv("MYTHOS_TTS_URL", "").rstrip("/")
    if not base:
        return (
            "ERROR: MYTHOS_TTS_URL is not set - run a TTS sidecar "
            "(e.g. `supertonic serve`, or `docker compose --profile voice up`) "
            "and set MYTHOS_TTS_URL=http://localhost:8000"
        )
    if not text.strip():
        return "ERROR: text is empty"

    body = json.dumps(
        {
            "model": os.getenv("MYTHOS_TTS_MODEL", "supertonic"),
            "input": text,
            "voice": voice,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/v1/audio/speech",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            audio = response.read(_MAX_AUDIO_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        return f"ERROR: HTTP {exc.code} from TTS sidecar: {detail}"
    except OSError as exc:
        return f"ERROR: TTS request failed: {exc}"

    if len(audio) > _MAX_AUDIO_BYTES:
        return f"ERROR: audio response exceeds the {_MAX_AUDIO_BYTES} byte cap"
    if not audio:
        return "ERROR: TTS sidecar returned an empty response"

    try:
        directory = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(directory, exist_ok=True)
        with open(output_path, "wb") as fh:
            fh.write(audio)
    except OSError as exc:
        return f"ERROR: could not write audio file: {exc}"
    return f"Wrote {len(audio)} bytes of audio to '{output_path}'"


TTS_TOOLS: List[Tool] = [
    Tool(
        name="speak",
        description=(
            "Convert text to spoken audio via the configured TTS service and "
            "write the audio file to the given path."
        ),
        parameters={
            "text": {"type": "string", "description": "The text to speak."},
            "output_path": {"type": "string", "description": "File path for the audio output."},
            "voice": {"type": "string", "description": "Voice/style name.", "default": "default"},
        },
        func=_tool_speak,
        required=["text", "output_path"],
    ),
]
