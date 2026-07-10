"""Back-compat alias: `mythos.tools_computer` → `mythos.tools.computer` (see the tools package)."""
import sys as _sys

from mythos.tools import computer as _mod

_sys.modules[__name__] = _mod
