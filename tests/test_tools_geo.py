"""
tests/test_tools_geo.py
-----------------------
Navigator tools over a mocked openrouteservice HTTP API.
"""
import io
import json
import urllib.error

import pytest

from mythos import tools_geo
from mythos.tools_geo import (
    _tool_ors_directions,
    _tool_ors_geocode,
    _tool_ors_matrix,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def install_api(monkeypatch, payload):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["data"] = request.data
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(tools_geo.urllib.request, "urlopen", fake_urlopen)
    return captured


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    monkeypatch.delenv("MYTHOS_ORS_URL", raising=False)


class TestKeyHandling:
    def test_missing_key_returns_guidance(self):
        result = _tool_ors_geocode("Haifa")
        assert result.startswith("ERROR:")
        assert "ORS_API_KEY" in result

    def test_self_hosted_url_needs_no_key(self, monkeypatch):
        monkeypatch.setenv("MYTHOS_ORS_URL", "http://localhost:8080/ors")
        captured = install_api(monkeypatch, {"features": [
            {"properties": {"label": "Haifa, Israel"},
             "geometry": {"coordinates": [34.99, 32.79]}},
        ]})
        result = _tool_ors_geocode("Haifa")
        assert "Haifa, Israel" in result
        assert captured["url"].startswith("http://localhost:8080/ors/geocode/search")


class TestTools:
    def test_geocode_compact_output(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "key-123")
        captured = install_api(monkeypatch, {"features": [
            {"properties": {"label": "Haifa, Israel"},
             "geometry": {"coordinates": [34.99, 32.79]}},
        ]})
        result = json.loads(_tool_ors_geocode("Haifa"))
        assert result == [{"label": "Haifa, Israel", "lon": 34.99, "lat": 32.79}]
        assert captured["headers"]["Authorization"] == "key-123"

    def test_geocode_no_results(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "k")
        install_api(monkeypatch, {"features": []})
        assert _tool_ors_geocode("Nowhereville").startswith("ERROR: no geocoding")

    def test_directions_summary(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "k")
        captured = install_api(monkeypatch, {"routes": [{
            "summary": {"distance": 1500.0, "duration": 120.0},
            "segments": [{"steps": [
                {"instruction": "Head north", "distance": 500.0},
            ]}],
        }]})
        result = json.loads(_tool_ors_directions("34.99,32.79", "34.78,32.07"))
        assert result["distance_m"] == 1500.0
        assert result["steps"][0]["instruction"] == "Head north"
        body = json.loads(captured["data"])
        assert body["coordinates"] == [[34.99, 32.79], [34.78, 32.07]]

    def test_directions_bad_coordinates(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "k")
        assert _tool_ors_directions("garbage", "34,32").startswith("ERROR:")

    def test_matrix_requires_two_locations(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "k")
        assert _tool_ors_matrix("34.99,32.79").startswith("ERROR:")

    def test_http_error_surfaced(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "k")

        def boom(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", {},
                io.BytesIO(b'{"error": "quota exceeded"}'),
            )

        monkeypatch.setattr(tools_geo.urllib.request, "urlopen", boom)
        result = _tool_ors_geocode("Haifa")
        assert "HTTP 403" in result
        assert "quota exceeded" in result

    def test_timeout_surfaced(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "k")

        def boom(request, timeout=None):
            raise OSError("timed out")

        monkeypatch.setattr(tools_geo.urllib.request, "urlopen", boom)
        assert _tool_ors_geocode("Haifa").startswith("ERROR:")
