"""Back-compat alias: `mythos.tools_browser` → `mythos.tools.browser` (see the tools package)."""
import sys as _sys

from mythos.tools import browser as _mod

_sys.modules[__name__] = _mod
