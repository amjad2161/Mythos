"""
tests/test_tools_tts.py
-----------------------
Voice tool over a mocked OpenAI-compatible TTS sidecar.
"""
import io
import json
import urllib.error

import pytest

from mythos import tools_tts
from mythos.tools_tts import _tool_speak


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MYTHOS_TTS_URL", raising=False)
    monkeypatch.delenv("MYTHOS_TTS_MODEL", raising=False)


def test_missing_url_returns_guidance(tmp_path):
    result = _tool_speak("hello", str(tmp_path / "a.wav"))
    assert result.startswith("ERROR:")
    assert "MYTHOS_TTS_URL" in result


def test_empty_text_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("MYTHOS_TTS_URL", "http://localhost:8000")
    assert _tool_speak("   ", str(tmp_path / "a.wav")).startswith("ERROR:")


def test_happy_path_writes_audio(monkeypatch, tmp_path):
    monkeypatch.setenv("MYTHOS_TTS_URL", "http://localhost:8000")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return FakeResponse(b"RIFF-fake-wav-bytes")

    monkeypatch.setattr(tools_tts.urllib.request, "urlopen", fake_urlopen)
    target = tmp_path / "out" / "hello.wav"
    result = _tool_speak("Turn left in 200 meters", str(target), voice="calm")

    assert result == f"Wrote 19 bytes of audio to '{target}'"
    assert target.read_bytes() == b"RIFF-fake-wav-bytes"
    assert captured["url"] == "http://localhost:8000/v1/audio/speech"
    assert captured["body"] == {
        "model": "supertonic",
        "input": "Turn left in 200 meters",
        "voice": "calm",
    }


def test_http_error_surfaced(monkeypatch, tmp_path):
    monkeypatch.setenv("MYTHOS_TTS_URL", "http://localhost:8000")

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 500, "boom", {}, io.BytesIO(b"model not loaded"),
        )

    monkeypatch.setattr(tools_tts.urllib.request, "urlopen", boom)
    result = _tool_speak("hi", str(tmp_path / "a.wav"))
    assert "HTTP 500" in result
    assert "model not loaded" in result


def test_connection_error_surfaced(monkeypatch, tmp_path):
    monkeypatch.setenv("MYTHOS_TTS_URL", "http://localhost:8000")

    def boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(tools_tts.urllib.request, "urlopen", boom)
    assert _tool_speak("hi", str(tmp_path / "a.wav")).startswith("ERROR:")


def test_empty_audio_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("MYTHOS_TTS_URL", "http://localhost:8000")
    monkeypatch.setattr(
        tools_tts.urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse(b""),
    )
    assert _tool_speak("hi", str(tmp_path / "a.wav")).startswith("ERROR:")
