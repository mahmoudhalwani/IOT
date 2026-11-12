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
from .media import ClipRecorder, MediaStore
from .store import Store

RESULT = {Outcome.GRANTED: "granted", Outcome.UNKNOWN: "denied_unknown",
          Outcome.SPOOF: "denied_spoof", Outcome.NOT_ALLOWED: "denied_not_allowed"}
NO_MEDIA = "recording is switched off"


class Controller:
    def __init__(self, cfg: Config, engine: FaceEngine, gallery: Gallery, store: Store,
                 lock, camera, motion=None, *, media: Optional[MediaStore] = None,
                 sleep=time.sleep, clock=time.monotonic, say=print):
        self.cfg, self.engine, self.gallery, self.store = cfg, engine, gallery, store
        self.lock, self.camera, self.motion = lock, camera, motion
        self._sleep, self._clock, self._say = sleep, clock, say
        self.media = media
        self._camera_ok = True
        self._frame = None                       # the frame the last decision was made on
        self._recorder: Optional[ClipRecorder] = None
        self._next_purge = 0.0
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
        if self.media is not None and self._clock() >= self._next_purge:
            self._next_purge = self._clock() + 600
            removed = self.media.purge()
            if removed:
                self._say(f"deleted {removed} old photo/clip file(s)")
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
        self._recorder = ClipRecorder(self.cfg, self._clock)
        while True:
            frame = self.camera.read()
            if frame is None:
                self._camera_fault()
                return None
            self._frame = frame
            self._recorder.add(frame)
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
        photo, photo_problem = self._save_photo()
        if d.outcome is Outcome.GRANTED:
            self._say(f"GRANTED {name} (similarity {d.similarity:.2f})")
            try:
                self.lock.unlock()
                self.store.add_event("face", "granted", name, photo=photo,
                                     clip_reason=photo_problem or "clips are kept for refused visitors only")
                self._sleep(self.cfg.relock_seconds)
            finally:
                self.lock.lock()                 # also runs if the program is stopped mid-wait
            return
        self._say(f"DENIED {RESULT[d.outcome]} {d.name or ''} {d.detail}".rstrip())
        # The log entry is written first (within 2 s of the decision, MH-06) and
        # names the clip that is about to be finished and saved.
        clip = MediaStore.new_id() if self.media is not None else None
        self.store.add_event("face", RESULT[d.outcome], name, d.detail, clip=clip,
                             clip_reason=None if clip else NO_MEDIA, photo=photo)
        if clip:
            self._finish_clip(clip)

    def _save_photo(self):
        """-> (photo id or None, reason there is none)."""
        if self.media is None:
            return None, NO_MEDIA
        if self._frame is None:
            return None, "camera failure"
        photo = MediaStore.new_id()
        if self.media.save_photo(photo, self._frame):
            return photo, None
        return None, "could not be saved"

    def _finish_clip(self, clip: str) -> None:
        """Keep filming until the clip is long enough, then store it encrypted."""
        rec = self._recorder
        while rec is not None and not rec.full:
            frame = self.camera.read()
            if frame is None:                    # camera died: keep what we have
                break
            rec.add(frame)
        if rec is None or not self.media.save_clip(clip, rec.frames):
            self._say("clip unavailable")

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
