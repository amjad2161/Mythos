"""
tests/test_tools_web.py
-----------------------
SSRF policy and fetch behaviour of the hardened web_fetch tool, plus the
think tool's presence in the default registry.
"""
import io
import urllib.error

import pytest

from mythos import tools_web
from mythos.tools import build_default_registry
from mythos.tools_web import _host_is_blocked, _tool_web_fetch


def fake_getaddrinfo(address):
    def _fake(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (address, 0))]
    return _fake


class TestHostPolicy:
    @pytest.mark.parametrize("address", [
        "127.0.0.1",        # loopback
        "10.1.2.3",         # RFC1918
        "192.168.1.1",      # RFC1918
        "169.254.169.254",  # link-local / metadata
        "::1",              # IPv6 loopback
    ])
    def test_non_public_addresses_blocked(self, monkeypatch, address):
        monkeypatch.setattr(tools_web.socket, "getaddrinfo", fake_getaddrinfo(address))
        assert _host_is_blocked("evil.example") is not None

    def test_public_address_allowed(self, monkeypatch):
        monkeypatch.setattr(tools_web.socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
        assert _host_is_blocked("example.com") is None

    def test_metadata_hostnames_blocked_by_name(self):
        assert _host_is_blocked("metadata.google.internal") is not None
        assert _host_is_blocked("169.254.169.254") is not None

    def test_unresolvable_host_blocked(self, monkeypatch):
        def boom(host, port, *args, **kwargs):
            raise OSError("NXDOMAIN")
        monkeypatch.setattr(tools_web.socket, "getaddrinfo", boom)
        assert "cannot resolve" in _host_is_blocked("nope.invalid")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TestWebFetch:
    def test_scheme_rejected(self):
        assert _tool_web_fetch("file:///etc/passwd").startswith("ERROR:")
        assert _tool_web_fetch("ftp://example.com/x").startswith("ERROR:")

    def test_private_target_rejected(self, monkeypatch):
        monkeypatch.setattr(tools_web.socket, "getaddrinfo", fake_getaddrinfo("10.0.0.5"))
        result = _tool_web_fetch("http://internal.example/secret")
        assert result.startswith("ERROR:")
        assert "non-public" in result

    def test_happy_path(self, monkeypatch):
        monkeypatch.setattr(tools_web.socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
        monkeypatch.setattr(
            tools_web._OPENER, "open",
            lambda req, timeout=None: FakeResponse(b"hello world"),
        )
        assert _tool_web_fetch("https://example.com/") == "hello world"

    def test_body_cap_and_notice(self, monkeypatch):
        monkeypatch.setattr(tools_web.socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
        monkeypatch.setattr(
            tools_web._OPENER, "open",
            lambda req, timeout=None: FakeResponse(b"x" * 500),
        )
        result = _tool_web_fetch("https://example.com/", max_bytes=100)
        assert result.startswith("x" * 100)
        assert "truncated at 100 bytes" in result

    def test_redirect_to_private_target_rejected(self, monkeypatch):
        # First hop is public and answers 302 -> private address.
        hosts = {"public.example": "93.184.216.34", "internal.example": "10.0.0.5"}

        def resolver(host, port, *args, **kwargs):
            return [(2, 1, 6, "", (hosts[host], 0))]

        def redirecting_open(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found",
                {"Location": "http://internal.example/secret"}, io.BytesIO(b""),
            )

        monkeypatch.setattr(tools_web.socket, "getaddrinfo", resolver)
        monkeypatch.setattr(tools_web._OPENER, "open", redirecting_open)
        result = _tool_web_fetch("http://public.example/start")
        assert result.startswith("ERROR:")
        assert "internal.example" in result

    def test_redirect_cap(self, monkeypatch):
        monkeypatch.setattr(tools_web.socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))

        def endless_redirect(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found",
                {"Location": "http://public.example/again"}, io.BytesIO(b""),
            )

        monkeypatch.setattr(tools_web._OPENER, "open", endless_redirect)
        result = _tool_web_fetch("http://public.example/start")
        assert "too many redirects" in result

    def test_network_error_returns_error_string(self, monkeypatch):
        monkeypatch.setattr(tools_web.socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))

        def boom(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(tools_web._OPENER, "open", boom)
        assert _tool_web_fetch("https://example.com/").startswith("ERROR:")


class TestRegistry:
    def test_new_tools_registered(self):
        registry = build_default_registry()
        for name in ("think", "web_fetch", "ors_geocode", "ors_directions",
                     "ors_isochrones", "ors_matrix", "speak"):
            assert registry.get(name) is not None, name

    def test_think_acknowledges(self):
        registry = build_default_registry()
        assert registry.call("think", {"thought": "plan first"}) == "Thought logged."
