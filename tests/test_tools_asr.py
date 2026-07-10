"""
tests/test_tools_asr.py
-----------------------
Voice-input (ASR) tool over a mocked OpenAI-compatible transcription sidecar.
"""
import io
import json
import urllib.error

import pytest

from mythos import tools_asr
from mythos.tools_asr import _tool_transcribe


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MYTHOS_ASR_URL", raising=False)
    monkeypatch.delenv("MYTHOS_ASR_MODEL", raising=False)


def test_missing_url_returns_guidance(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFFfake")
    result = _tool_transcribe(str(audio))
    assert result.startswith("ERROR:")
    assert "MYTHOS_ASR_URL" in result


def test_missing_file(monkeypatch):
    monkeypatch.setenv("MYTHOS_ASR_URL", "http://localhost:8001")
    assert _tool_transcribe("/no/such/audio.wav").startswith("ERROR:")


def test_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MYTHOS_ASR_URL", "http://localhost:8001")
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF" + b"\x00" * 100)
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["ctype"] = request.headers.get("Content-type", "")
        return FakeResponse(json.dumps({"text": "plan the route to Eilat"}).encode())

    monkeypatch.setattr(tools_asr.urllib.request, "urlopen", fake_urlopen)
    result = _tool_transcribe(str(audio))
    assert result == "plan the route to Eilat"
    assert captured["url"] == "http://localhost:8001/v1/audio/transcriptions"
    assert captured["ctype"].startswith("multipart/form-data; boundary=")


def test_http_error_surfaced(monkeypatch, tmp_path):
    monkeypatch.setenv("MYTHOS_ASR_URL", "http://localhost:8001")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {},
                                     io.BytesIO(b"model not loaded"))

    monkeypatch.setattr(tools_asr.urllib.request, "urlopen", boom)
    result = _tool_transcribe(str(audio))
    assert "HTTP 500" in result


def test_registered_and_on_voice_role():
    from mythos.tools import build_default_registry
    from mythos.orchestration.roles import build_registry_for_role

    assert build_default_registry().get("transcribe") is not None
    assert build_registry_for_role("voice").get("transcribe") is not None
