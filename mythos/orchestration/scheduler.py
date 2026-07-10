"""
mythos/orchestration/scheduler.py
---------------------------------
Always-on routine scheduler — the proactive heartbeat (JARVIS_BLUEPRINT §4.4).

Turns Mythos from purely reactive into proactive: declarative **routines** fire
goals onto the swarm on a schedule (interval or daily-at) or on demand, with
quiet-hours suppression and durable persistence so they survive a restart.

The scheduling core is pure and clock-injectable (``due_routines`` / ``tick``),
so it is fully unit-testable without real time; the background thread is a thin
loop that calls ``tick`` with the wall clock.  A routine "fires" by invoking a
``fire(routine)`` callback — the runtime wires that to submit the routine's goal
to a :class:`~mythos.orchestration.runtime.SwarmRuntime`; the scheduler itself
stays decoupled from how work is executed.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional

_POLL_S = 30.0


@dataclass
class Routine:
    """A declarative scheduled goal."""

    id: str
    goal: str
    name: str = ""
    interval_s: float = 0.0        # > 0 → fire every N seconds
    daily_at: str = ""             # "HH:MM" (UTC) → fire once per day at that time
    enabled: bool = True
    quiet_start: int = -1          # hour [0-23]; -1 = no quiet window
    quiet_end: int = -1
    access_level: str = "standard"
    last_fired: float = 0.0        # epoch seconds of the last firing

    def in_quiet_hours(self, hour: int) -> bool:
        if self.quiet_start < 0 or self.quiet_end < 0:
            return False
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= hour < self.quiet_end
        # wrap-around window (e.g. 22 → 6)
        return hour >= self.quiet_start or hour < self.quiet_end


def _is_due(routine: Routine, now: float) -> bool:
    if not routine.enabled:
        return False
    now_tm = time.gmtime(now)
    if routine.in_quiet_hours(now_tm.tm_hour):
        return False
    if routine.interval_s > 0:
        return (now - routine.last_fired) >= routine.interval_s
    if routine.daily_at:
        try:
            hh, mm = (int(x) for x in routine.daily_at.split(":", 1))
        except ValueError:
            return False
        target = now - (now_tm.tm_hour * 3600 + now_tm.tm_min * 60 + now_tm.tm_sec) \
            + hh * 3600 + mm * 60
        # due once we've passed today's target and haven't fired since it
        return now >= target and routine.last_fired < target
    return False


def due_routines(routines: List[Routine], now: float) -> List[Routine]:
    """Pure: the subset of *routines* that should fire at *now* (epoch)."""
    return [r for r in routines if _is_due(r, now)]


class Scheduler:
    """Owns routines and drives them; the firing mechanism is injected."""

    def __init__(
        self,
        fire: Callable[[Routine], None],
        clock: Callable[[], float] = time.time,
        poll_s: float = _POLL_S,
        path: str = "",
    ) -> None:
        self._fire = fire
        self._clock = clock
        self._poll_s = poll_s
        self._path = path or os.getenv("MYTHOS_ROUTINES_PATH", "")
        self._routines: Dict[str, Routine] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if self._path:
            self.load()

    # -- routine management ------------------------------------------------
    def add(self, routine: Routine) -> None:
        with self._lock:
            self._routines[routine.id] = routine
            self._persist()

    def remove(self, routine_id: str) -> bool:
        with self._lock:
            existed = self._routines.pop(routine_id, None) is not None
            if existed:
                self._persist()
            return existed

    def list(self) -> List[Routine]:
        with self._lock:
            return list(self._routines.values())

    # -- driving -----------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> List[Routine]:
        """Fire every due routine once; return those fired. Errors are isolated."""
        stamp = self._clock() if now is None else now
        with self._lock:
            due = due_routines(list(self._routines.values()), stamp)
            for routine in due:
                routine.last_fired = stamp
            if due:
                self._persist()
        fired: List[Routine] = []
        for routine in due:
            try:
                self._fire(routine)
                fired.append(routine)
            except Exception:  # noqa: BLE001 – one bad routine never stops the rest
                pass
        return fired

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mythos-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._poll_s)

    # -- persistence -------------------------------------------------------
    def _persist(self) -> None:
        # Caller holds the lock. Best-effort; never raise into the scheduler.
        if not self._path:
            return
        try:
            directory = os.path.dirname(os.path.abspath(self._path))
            os.makedirs(directory, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as handle:
                json.dump([asdict(r) for r in self._routines.values()], handle, indent=2)
        except OSError:
            pass

    def load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        with self._lock:
            for row in data if isinstance(data, list) else []:
                try:
                    routine = Routine(**row)
                except TypeError:
                    continue
                self._routines[routine.id] = routine


def load_routines(path: str) -> List[Routine]:
    """Load routines from a JSON file (list of routine objects)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    out: List[Routine] = []
    for row in data if isinstance(data, list) else []:
        try:
            out.append(Routine(**row))
        except TypeError:
            continue
    return out
