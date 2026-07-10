"""
mythos/tools_web.py
-------------------
SSRF-hardened web fetching for agents with network egress.

Policy (every failure path returns a structured ``"ERROR: ..."`` string,
never raises):

* schemes: http/https only;
* the target host of EVERY hop (initial URL and each redirect) is resolved
  and every A/AAAA record must be public — loopback, private (RFC 1918),
  link-local, reserved, multicast, and unspecified addresses are refused,
  as are the well-known cloud metadata endpoints;
* redirects are followed manually (max 5) so each hop is re-validated;
* response bodies are capped (100 kB) and truncated with a notice.

Known limitation (stdlib): validation resolves DNS separately from the
actual connection, so a DNS-rebinding attacker with sub-second TTLs could
theoretically pass validation and connect elsewhere (TOCTOU).  Blocking that
requires pinning the validated IP through the TLS layer — out of scope here.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

from .tools import Tool, _truncate

_ALLOWED_SCHEMES = ("http", "https")
_BLOCKED_HOSTS = {
    "169.254.169.254",          # AWS/GCP/Azure metadata
    "metadata.google.internal",  # GCP metadata alias
    "100.100.100.200",           # Alibaba Cloud metadata
    "fd00:ec2::254",             # AWS IPv6 metadata
}
_MAX_REDIRECTS = 5
_MAX_BODY_BYTES = 100_000
_TIMEOUT_S = 15


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse automatic redirects – we validate and follow them manually."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _host_is_blocked(host: str) -> Optional[str]:
    """Return a refusal reason when *host* must not be fetched, else None."""
    if not host:
        return "URL has no host"
    if host.lower().rstrip(".") in _BLOCKED_HOSTS:
        return f"host '{host}' is a blocked metadata endpoint"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        return f"cannot resolve host '{host}': {exc}"
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return f"host '{host}' resolves to non-public address {address}"
    return None


def _validate_url(url: str) -> Tuple[Optional[str], Optional[urllib.parse.ParseResult]]:
    """Return (refusal reason, parsed url)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"scheme '{parsed.scheme or '(none)'}' is not allowed (http/https only)", None
    reason = _host_is_blocked(parsed.hostname or "")
    if reason:
        return reason, None
    return None, parsed


def _tool_web_fetch(url: str, max_bytes: int = _MAX_BODY_BYTES) -> str:
    """Fetch *url* with SSRF protections and return the (capped) body text."""
    try:
        max_bytes = max(1, min(int(max_bytes), _MAX_BODY_BYTES))
    except (TypeError, ValueError):
        max_bytes = _MAX_BODY_BYTES

    visited: List[str] = []
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        reason, parsed = _validate_url(current)
        if reason:
            return f"ERROR: refusing to fetch {current!r}: {reason}"
        visited.append(current)

        request = urllib.request.Request(
            current, headers={"User-Agent": "mythos-agent/1.0"}
        )
        try:
            response = _OPENER.open(request, timeout=_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                if not location:
                    return f"ERROR: redirect ({exc.code}) with no Location header"
                current = urllib.parse.urljoin(current, location)
                continue
            body = exc.read(2000).decode("utf-8", errors="replace")
            return f"ERROR: HTTP {exc.code} from {current}: {body}"
        except OSError as exc:
            return f"ERROR: fetch failed for {current}: {exc}"

        with response:
            data = response.read(max_bytes + 1)
        text = data.decode("utf-8", errors="replace")
        if len(data) > max_bytes:
            text = text[:max_bytes] + f"\n… [truncated at {max_bytes} bytes]"
        return _truncate(text)

    return f"ERROR: too many redirects (>{_MAX_REDIRECTS}): {' -> '.join(visited)}"


WEB_TOOLS: List[Tool] = [
    Tool(
        name="web_fetch",
        description=(
            "Fetch a public http(s) URL and return the response body text "
            "(capped). Private/internal addresses and cloud metadata "
            "endpoints are refused."
        ),
        parameters={
            "url": {"type": "string", "description": "The http(s) URL to fetch."},
            "max_bytes": {
                "type": "integer",
                "description": f"Response cap in bytes (default and max {_MAX_BODY_BYTES}).",
                "default": _MAX_BODY_BYTES,
            },
        },
        func=_tool_web_fetch,
        required=["url"],
    ),
]
