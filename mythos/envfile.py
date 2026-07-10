"""Back-compat alias: `mythos.envfile` → `mythos.pc.envfile`."""
import sys as _sys

from mythos.pc import envfile as _mod

_sys.modules[__name__] = _mod
