"""
mythos/tools_asr.py
-------------------
Speech-to-text (ASR) tool — the input half of the voice interface.

Symmetric with ``tools_tts.py``: talks to any OpenAI-compatible
``/v1/audio/transcriptions`` sidecar (e.g. faster-whisper-server). Voice
becomes an input *method at the human boundary* — the transcript is returned
for confirmation, never auto-executed — keeping the rule that natural language
lives only at the edges, never between machines.

Config:
* ``MYTHOS_ASR_URL``   – base URL of the sidecar (e.g. ``http://localhost:8001``).
* ``MYTHOS_ASR_MODEL`` – model name (default ``whisper-1``).
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from typing import List

from . import Tool, _truncate

_TIMEOUT_S = 120
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB


def _multipart(fields: dict, file_field: str, filename: str, data: bytes) -> "tuple[bytes, str]":
    """Build a minimal multipart/form-data body (stdlib only)."""
    boundary = f"----mythos{uuid.uuid4().hex}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts: List[bytes] = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def _tool_transcribe(audio_path: str) -> str:
    """Transcribe the audio file at *audio_path* to text via the ASR sidecar."""
    base = os.getenv("MYTHOS_ASR_URL", "").rstrip("/")
    if not base:
        return (
            "ERROR: MYTHOS_ASR_URL is not set - run an ASR sidecar "
            "(e.g. faster-whisper-server, or `docker compose --profile voice-in up`) "
            "and set MYTHOS_ASR_URL=http://localhost:8001"
        )
    if not os.path.isfile(audio_path):
        return f"ERROR: audio file not found: {audio_path}"
    try:
        size = os.path.getsize(audio_path)
        if size > _MAX_AUDIO_BYTES:
            return f"ERROR: audio exceeds the {_MAX_AUDIO_BYTES} byte cap"
        with open(audio_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return f"ERROR: could not read audio: {exc}"

    body, boundary = _multipart(
        {"model": os.getenv("MYTHOS_ASR_MODEL", "whisper-1")},
        "file", os.path.basename(audio_path), data,
    )
    request = urllib.request.Request(
        f"{base}/v1/audio/transcriptions",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        return f"ERROR: HTTP {exc.code} from ASR sidecar: {detail}"
    except OSError as exc:
        return f"ERROR: ASR request failed: {exc}"

    try:
        text = json.loads(raw).get("text", "")
    except ValueError:
        text = raw.decode("utf-8", errors="replace")
    return _truncate(text.strip()) or "ERROR: ASR returned an empty transcript"


ASR_TOOLS: List[Tool] = [
    Tool(
        name="transcribe",
        description="Transcribe an audio file to text via the configured ASR service.",
        parameters={
            "audio_path": {"type": "string", "description": "Path to the audio file."},
        },
        func=_tool_transcribe,
        required=["audio_path"],
    ),
]
