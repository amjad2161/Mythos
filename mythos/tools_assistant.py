"""Back-compat alias: `mythos.tools_assistant` → `mythos.tools.assistant` (see the tools package)."""
import sys as _sys

from mythos.tools import assistant as _mod

_sys.modules[__name__] = _mod
