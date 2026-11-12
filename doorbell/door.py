"""One door, two things that can open it: the face loop and the website.
This wrapper makes lock commands thread-safe and gives remote unlocks the same
automatic re-lock as face unlocks (MH-05)."""
from __future__ import annotations

import threading
import time
from typing import Optional


class Door:
    def __init__(self, lock, relock_seconds: float):
        self._lock_hw = lock
        self._relock_seconds = relock_seconds
        self._mutex = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self.locked: Optional[bool] = None       # None = not driven yet
        self.changed_at = time.time()

    def lock(self) -> None:
        with self._mutex:
            self._cancel_timer()
            self._lock_hw.lock()
            self._set(True)

    def unlock(self) -> None:
        """Used by the face loop, which does its own timed re-lock."""
        with self._mutex:
            self._lock_hw.unlock()
            self._set(False)

    def unlock_then_relock(self) -> None:
        """Used by the website: unlock now, lock again by itself."""
        with self._mutex:
            self._cancel_timer()
            self.unlock()
            self._timer = threading.Timer(self._relock_seconds, self.lock)
            self._timer.daemon = True
            self._timer.start()

    def status(self) -> dict:
        state = "unknown" if self.locked is None else ("locked" if self.locked else "unlocked")
        return {"state": state, "since": int(self.changed_at),
                "relock_seconds": self._relock_seconds,
                # There is no door sensor in the prototype: this is the last position
                # the servo was sent to, not a measured one (stated limit, MH-04).
                "confirmed": False}

    def close(self) -> None:
        with self._mutex:
            self._cancel_timer()
            self._lock_hw.close()

    def _set(self, locked: bool) -> None:
        self.locked, self.changed_at = locked, time.time()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
