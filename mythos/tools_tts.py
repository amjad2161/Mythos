"""Back-compat alias: `mythos.tools_tts` → `mythos.tools.tts` (see the tools package)."""
import sys as _sys

from mythos.tools import tts as _mod

_sys.modules[__name__] = _mod
