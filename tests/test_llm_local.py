"""
tests/test_llm_local.py
-----------------------
The dependency-free local / OpenAI-compatible provider (Ollama, LM Studio,
llama.cpp, vLLM, Groq) — happy path, tool-call parsing, usage mapping, and
transient-error surfacing — all over a mocked urllib.
"""
import io
import json
import urllib.error

import pytest

from mythos import llm as llm_mod
from mythos.llm import LocalLLM, RetryingLLM, create_llm


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _json_response(payload):
    return FakeResponse(json.dumps(payload).encode("utf-8"))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("MYTHOS_LOCAL_URL", "MYTHOS_LOCAL_API_KEY", "MYTHOS_LOCAL_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)


def test_factory_returns_local_for_local_and_ollama():
    assert isinstance(create_llm("local", "llama3.3", None), LocalLLM)
    assert isinstance(create_llm("ollama", "llama3.3", None), LocalLLM)


def test_default_url_targets_ollama():
    client = LocalLLM("llama3.3")
    assert client._url == "http://localhost:11434/v1/chat/completions"


def test_custom_base_url_env(monkeypatch):
    monkeypatch.setenv("MYTHOS_LOCAL_URL", "http://box:8000/v1")
    assert LocalLLM("m")._url == "http://box:8000/v1/chat/completions"


def test_plain_content_response(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["auth"] = request.headers.get("Authorization")
        return _json_response({
            "choices": [{"message": {"content": "hello there"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5},
        })

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    resp = LocalLLM("llama3.3").chat([{"role": "user", "content": "hi"}])

    assert resp.content == "hello there"
    assert not resp.has_tool_call
    assert resp.usage == {"input": 11, "output": 5, "cache_read": 0, "cache_creation": 0}
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["body"]["model"] == "llama3.3"
    assert captured["auth"] == "Bearer local"


def test_tool_call_response(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _json_response({
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {"name": "calculate", "arguments": '{"expression": "2+2"}'},
                    }],
                },
            }],
        })

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    resp = LocalLLM("llama3.3").chat(
        [{"role": "user", "content": "add"}],
        tools=[{"type": "function", "function": {"name": "calculate"}}],
    )
    assert resp.has_tool_call
    assert resp.tool_name == "calculate"
    assert resp.tool_args == {"expression": "2+2"}
    assert resp.tool_call_id == "call_1"


def test_tools_included_in_payload(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _json_response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    LocalLLM("m").chat(
        [{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "t"}}],
    )
    assert captured["body"]["tool_choice"] == "auto"
    assert captured["body"]["tools"][0]["function"]["name"] == "t"


def test_http_error_raises_runtime(monkeypatch):
    def boom(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 500, "err", {}, io.BytesIO(b"model missing")
        )

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="Local LLM HTTP 500"):
        LocalLLM("m").chat([{"role": "user", "content": "x"}])


def test_connection_error_is_transient_for_retry(monkeypatch):
    calls = {"n": 0}

    def boom(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("connection refused")
        return _json_response({"choices": [{"message": {"content": "recovered"}}]})

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", boom)
    # RetryingLLM should treat the "connection failed" RuntimeError as transient
    # (base_delay=0 keeps the single backoff sleep negligible).
    client = RetryingLLM(LocalLLM("m"), attempts=2, base_delay=0, jitter=0)
    resp = client.chat([{"role": "user", "content": "x"}])
    assert resp.content == "recovered"
    assert calls["n"] == 2
