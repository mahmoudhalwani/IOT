"""The door's main loop.

    IDLE  --motion-->  CHECKING  --granted-->  UNLOCKED (5 s)  -->  LOCKED
                           |                                         |
                           +--denied / nobody there------------------+--> wait until clear --> IDLE

Safety rules (MH-08): the door is driven to "locked" when the program starts and
whenever it stops, and every failure path leaves it locked.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .config import Config
from .decision import Decision, Outcome, Visit
from .face import FaceEngine, Gallery
from .store import Store

RESULT = {Outcome.GRANTED: "granted", Outcome.UNKNOWN: "denied_unknown",
          Outcome.SPOOF: "denied_spoof", Outcome.NOT_ALLOWED: "denied_not_allowed"}
NO_CLIP = "clip recording not enabled yet"


class Controller:
    def __init__(self, cfg: Config, engine: FaceEngine, gallery: Gallery, store: Store,
                 lock, camera, motion=None, *, sleep=time.sleep, clock=time.monotonic, say=print):
        self.cfg, self.engine, self.gallery, self.store = cfg, engine, gallery, store
        self.lock, self.camera, self.motion = lock, camera, motion
        self._sleep, self._clock, self._say = sleep, clock, say
        self._camera_ok = True
        self.stop = threading.Event()            # set from another thread to end run()

    # ---- top level --------------------------------------------------------
    def run(self) -> None:
        self.lock.lock()                         # never trust the state we woke up in
        if not self.camera.open():
            self._camera_fault()
        self._say("ready")
        try:
            while not self.stop.is_set():
                self.cycle()
        finally:
            self.lock.lock()
            self.camera.close()

    def cycle(self) -> Optional[Decision]:
        """Wait briefly for a trigger; if there is one, handle the whole visit."""
        if not self._triggered():
            return None
        decision = self._visit()
        if decision is not None:
            self._act(decision)
            self._wait_until_clear()
            self._say("ready")
        return decision

    # ---- steps ------------------------------------------------------------
    def _triggered(self) -> bool:
        if not self._camera_ok:
            self._sleep(self.cfg.camera_retry_seconds)
            if self.camera.open():
                self._camera_ok = True
                self._say("camera is back")
            return False
        if self.motion is not None:
            self.camera.skip()                   # keep the camera's buffer fresh
            if self.motion.detected():
                return True
            self._sleep(0.05)
            return False
        frame = self.camera.read()               # no sensor: a face in view is the trigger
        if frame is None:
            self._camera_fault()
            return False
        if self.engine.detect(frame):
            return True
        self._sleep(0.15)
        return False

    def _visit(self) -> Optional[Decision]:
        """Watch one visitor until there is a final decision. None = nobody came
        to the camera (motion without a face), which is not an entry attempt."""
        self.gallery.load(self.store.templates())     # pick up people added or removed
        visit = Visit(self.engine, self.gallery, self.store, self.cfg)
        start = last_face = self._clock()
        seen_face = False
        while True:
            frame = self.camera.read()
            if frame is None:
                self._camera_fault()
                return None
            d = visit.update(frame)
            now = self._clock()
            if d.final:
                return d
            if d.outcome is not Outcome.NO_FACE:
                last_face, seen_face = now, True
            if now - last_face > self.cfg.visit_idle_seconds:
                return None
            if seen_face and now - start > self.cfg.visit_max_seconds:
                return Decision(Outcome.UNKNOWN, detail="no clear view of the face in time")

    def _act(self, d: Decision) -> None:
        name = d.name or "unknown"
        if d.outcome is Outcome.GRANTED:
            self._say(f"GRANTED {name} (similarity {d.similarity:.2f})")
            try:
                self.lock.unlock()
                self.store.add_event("face", "granted", name, clip_reason=NO_CLIP)
                self._sleep(self.cfg.relock_seconds)
            finally:
                self.lock.lock()                 # also runs if the program is stopped mid-wait
        else:
            self._say(f"DENIED {RESULT[d.outcome]} {d.name or ''} {d.detail}".rstrip())
            self.store.add_event("face", RESULT[d.outcome], name, d.detail, clip_reason=NO_CLIP)

    def _wait_until_clear(self) -> None:
        """Do not judge the same person again while they are still at the door."""
        deadline = self._clock() + self.cfg.clear_max_seconds
        clear = 0
        while clear < 10 and self._clock() < deadline:
            frame = self.camera.read()
            if frame is None:
                return
            busy = bool(self.engine.detect(frame)) or (self.motion is not None and self.motion.detected())
            clear = 0 if busy else clear + 1

    def _camera_fault(self) -> None:
        if self._camera_ok:                      # log once per outage, not once per retry
            self._camera_ok = False
            self._say("FAULT camera not working; door stays locked")
            self.store.add_event("system", "fault", "unknown", "camera failure",
                                 clip_reason="camera failure")
        self.camera.close()
