"""Back-compat alias: `mythos.tools_asr` → `mythos.tools.asr` (see the tools package)."""
import sys as _sys

from mythos.tools import asr as _mod

_sys.modules[__name__] = _mod
