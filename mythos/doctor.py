"""Back-compat alias: `mythos.doctor` → `mythos.pc.doctor`."""
import sys as _sys

from mythos.pc import doctor as _mod

_sys.modules[__name__] = _mod
