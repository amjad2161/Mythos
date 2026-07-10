"""Back-compat alias: `mythos.tools_web` → `mythos.tools.web` (see the tools package)."""
import sys as _sys

from mythos.tools import web as _mod

_sys.modules[__name__] = _mod
