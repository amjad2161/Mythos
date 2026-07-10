"""Back-compat alias: `mythos.tools_geo` → `mythos.tools.geo` (see the tools package)."""
import sys as _sys

from mythos.tools import geo as _mod

_sys.modules[__name__] = _mod
