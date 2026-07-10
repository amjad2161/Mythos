"""
mythos/tools_geo.py
-------------------
Geographic tools for the navigator agent role, backed by the
openrouteservice REST API (hosted at api.openrouteservice.org with a free
API key, or self-hosted — see https://openrouteservice.org).

Implemented as a thin stdlib ``urllib`` wrapper (no extra dependency).
Configuration:

* ``ORS_API_KEY``    – API key (required for the hosted service).
* ``MYTHOS_ORS_URL`` – base URL override for a self-hosted instance
  (default ``https://api.openrouteservice.org``).

Every failure path — missing key, HTTP error, timeout, malformed JSON —
returns a structured ``"ERROR: ..."`` string so the agent loop never crashes,
and results are compact JSON summaries (not raw ORS dumps) to protect the
context window.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import Tool, _truncate

_DEFAULT_BASE = "https://api.openrouteservice.org"
_TIMEOUT_S = 20


def _ors_base() -> str:
    return os.getenv("MYTHOS_ORS_URL", _DEFAULT_BASE).rstrip("/")


def _ors_key() -> Optional[str]:
    return os.getenv("ORS_API_KEY")


def _ors_request(
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    query: Optional[Dict[str, str]] = None,
) -> Any:
    """GET (payload=None) or POST JSON to the ORS API; raise ValueError on failure."""
    key = _ors_key()
    if not key and _ors_base() == _DEFAULT_BASE:
        raise ValueError(
            "ORS_API_KEY is not set. Get a free key at https://openrouteservice.org "
            "or point MYTHOS_ORS_URL at a self-hosted instance."
        )
    url = f"{_ors_base()}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = key
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise ValueError(f"HTTP {exc.code} from openrouteservice: {detail}") from exc
    except OSError as exc:
        raise ValueError(f"openrouteservice request failed: {exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ValueError(f"openrouteservice returned invalid JSON: {exc}") from exc


def _parse_lonlat(text: str) -> List[float]:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        raise ValueError(f"expected 'lon,lat', got {text!r}")
    return [float(parts[0]), float(parts[1])]


def _compact(obj: Any) -> str:
    return _truncate(json.dumps(obj, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _tool_ors_geocode(text: str, size: int = 3) -> str:
    """Geocode a place name to coordinates (top *size* candidates)."""
    try:
        result = _ors_request(
            "/geocode/search", query={"text": text, "size": str(max(1, min(size, 10)))}
        )
        candidates = [
            {
                "label": f.get("properties", {}).get("label", ""),
                "lon": f.get("geometry", {}).get("coordinates", [None, None])[0],
                "lat": f.get("geometry", {}).get("coordinates", [None, None])[1],
            }
            for f in result.get("features", [])
        ]
        if not candidates:
            return f"ERROR: no geocoding results for {text!r}"
        return _compact(candidates)
    except (ValueError, TypeError, KeyError) as exc:
        return f"ERROR: {exc}"


def _tool_ors_directions(start: str, end: str, profile: str = "driving-car") -> str:
    """Route from *start* to *end* ('lon,lat' each); returns distance/duration/steps."""
    try:
        body = {"coordinates": [_parse_lonlat(start), _parse_lonlat(end)]}
        result = _ors_request(f"/v2/directions/{profile}", payload=body)
        route = result["routes"][0]
        summary = route.get("summary", {})
        steps = [
            {"instruction": s.get("instruction", ""), "distance_m": s.get("distance")}
            for seg in route.get("segments", [])
            for s in seg.get("steps", [])
        ]
        return _compact(
            {
                "distance_m": summary.get("distance"),
                "duration_s": summary.get("duration"),
                "steps": steps[:40],
            }
        )
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        return f"ERROR: {exc}"


def _tool_ors_isochrones(
    location: str, range_seconds: int, profile: str = "driving-car"
) -> str:
    """Reachability polygon(s) around *location* ('lon,lat') within *range_seconds*."""
    try:
        body = {
            "locations": [_parse_lonlat(location)],
            "range": [max(60, int(range_seconds))],
        }
        result = _ors_request(f"/v2/isochrones/{profile}", payload=body)
        features = [
            {
                "value_s": f.get("properties", {}).get("value"),
                "area_center": f.get("properties", {}).get("center"),
                "polygon_points": len(
                    (f.get("geometry", {}).get("coordinates") or [[]])[0]
                ),
            }
            for f in result.get("features", [])
        ]
        return _compact(features)
    except (ValueError, TypeError, KeyError) as exc:
        return f"ERROR: {exc}"


def _tool_ors_matrix(locations: str, profile: str = "driving-car") -> str:
    """Duration/distance matrix between locations ('lon,lat' pairs, ';'-separated)."""
    try:
        coords = [_parse_lonlat(pair) for pair in locations.split(";") if pair.strip()]
        if len(coords) < 2:
            return "ERROR: matrix needs at least two 'lon,lat' locations separated by ';'"
        body = {"locations": coords, "metrics": ["duration", "distance"]}
        result = _ors_request(f"/v2/matrix/{profile}", payload=body)
        return _compact(
            {
                "durations_s": result.get("durations"),
                "distances_m": result.get("distances"),
            }
        )
    except (ValueError, TypeError, KeyError) as exc:
        return f"ERROR: {exc}"


GEO_TOOLS: List[Tool] = [
    Tool(
        name="ors_geocode",
        description="Geocode a place name/address to coordinates via openrouteservice.",
        parameters={
            "text": {"type": "string", "description": "Place name or address to geocode."},
            "size": {"type": "integer", "description": "Max candidates (default 3).", "default": 3},
        },
        func=_tool_ors_geocode,
        required=["text"],
    ),
    Tool(
        name="ors_directions",
        description=(
            "Compute a route between two coordinates. Returns distance (m), "
            "duration (s), and turn-by-turn steps."
        ),
        parameters={
            "start": {"type": "string", "description": "Start as 'lon,lat'."},
            "end": {"type": "string", "description": "End as 'lon,lat'."},
            "profile": {
                "type": "string",
                "description": "Routing profile: driving-car, cycling-regular, foot-walking …",
                "default": "driving-car",
            },
        },
        func=_tool_ors_directions,
        required=["start", "end"],
    ),
    Tool(
        name="ors_isochrones",
        description="Compute the area reachable from a point within a time budget.",
        parameters={
            "location": {"type": "string", "description": "Center as 'lon,lat'."},
            "range_seconds": {"type": "integer", "description": "Travel-time budget in seconds."},
            "profile": {"type": "string", "description": "Routing profile.", "default": "driving-car"},
        },
        func=_tool_ors_isochrones,
        required=["location", "range_seconds"],
    ),
    Tool(
        name="ors_matrix",
        description="Compute a travel time/distance matrix between multiple coordinates.",
        parameters={
            "locations": {
                "type": "string",
                "description": "Locations as 'lon,lat' pairs separated by ';'.",
            },
            "profile": {"type": "string", "description": "Routing profile.", "default": "driving-car"},
        },
        func=_tool_ors_matrix,
        required=["locations"],
    ),
]
