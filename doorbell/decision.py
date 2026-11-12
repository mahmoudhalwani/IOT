"""Turns a stream of camera frames from one visit into a single decision.

Order matters: liveness is checked before identity, so a photo of a resident is
never even compared with the database. Unlocking needs several consecutive
frames that are all live AND all the same person; one bad frame resets the run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

import numpy as np

from .config import Config
from .face import FaceEngine, Gallery
from .store import Store


class Outcome(str, Enum):
    NO_FACE = "no_face"            # nobody in view
    POSITION = "position"          # face too far away or cut off by the frame edge
    CHECKING = "checking"          # still collecting frames
    GRANTED = "granted"            # final: unlock
    UNKNOWN = "unknown"            # final: real person, not enrolled
    SPOOF = "spoof"                # final: photo or screen shown to the camera
    NOT_ALLOWED = "not_allowed"    # final: known person, outside their allowed time

FINAL = {Outcome.GRANTED, Outcome.UNKNOWN, Outcome.SPOOF, Outcome.NOT_ALLOWED}


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    person_id: Optional[int] = None
    name: Optional[str] = None
    similarity: float = 0.0
    p_real: float = 0.0
    detail: str = ""

    @property
    def final(self) -> bool:
        return self.outcome in FINAL


class Visit:
    """One person standing at the door. Create a new Visit for each motion event."""

    def __init__(self, engine: FaceEngine, gallery: Gallery, store: Store, cfg: Config):
        self.engine, self.gallery, self.store, self.cfg = engine, gallery, store, cfg
        self._streak_person: Optional[int] = None
        self._streak = 0
        self._spoof_run = 0        # spoof frames in a row
        self._spoof_total = 0      # spoof frames in the whole visit
        self._unknown_frames = 0
        self._gap = 0              # frames in a row with no usable face

    def update(self, frame: np.ndarray, now: Optional[datetime] = None) -> Decision:
        faces = self.engine.detect(frame)
        if not faces:
            self._no_usable_face()
            return Decision(Outcome.NO_FACE)
        face = faces[0]
        problem = self.engine.placement_problem(frame, face)
        if problem:
            self._no_usable_face()
            return Decision(Outcome.POSITION, detail=problem)
        self._gap = 0

        live = self.engine.liveness(frame, face)
        if not live.passed(self.cfg.live_threshold):
            # One doubtful frame only restarts the count; a real attack fails
            # frame after frame, which is what turns the visit into SPOOF.
            self._reset_streak()
            self._spoof_run += 1
            self._spoof_total += 1
            if (self._spoof_run >= self.cfg.spoof_frames_to_deny
                    or self._spoof_total >= self.cfg.spoof_frames_total):
                return Decision(Outcome.SPOOF, p_real=live.p_real, detail="photo or screen detected")
            return Decision(Outcome.CHECKING, p_real=live.p_real)
        self._spoof_run = 0

        person_id, sim = self.gallery.match(self.engine.embed(frame, face))
        if person_id is None:
            self._reset_streak()
            self._unknown_frames += 1
            if self._unknown_frames >= self.cfg.unknown_frames_to_deny:
                return Decision(Outcome.UNKNOWN, similarity=sim, p_real=live.p_real)
            return Decision(Outcome.CHECKING, similarity=sim, p_real=live.p_real)

        if person_id != self._streak_person:
            self._streak_person, self._streak = person_id, 0
        self._streak += 1
        if self._streak < self.cfg.live_frames_required:
            return Decision(Outcome.CHECKING, person_id, similarity=sim, p_real=live.p_real)

        person = self.store.person(person_id)
        if person is None or not person.enabled:       # removed while standing at the door
            self._reset_streak()
            return Decision(Outcome.UNKNOWN, similarity=sim, p_real=live.p_real)
        ok, why = person.rule.check(now or datetime.now())
        outcome = Outcome.GRANTED if ok else Outcome.NOT_ALLOWED
        return Decision(outcome, person.id, person.name, sim, live.p_real, why)

    def _no_usable_face(self) -> None:
        """A blink, a head turn or a step sideways should not throw away good
        frames; a longer absence means the person left, so start again."""
        self._gap += 1
        if self._gap > self.cfg.gap_frames_allowed:
            self._reset_streak()
            self._spoof_run = 0

    def _reset_streak(self) -> None:
        self._streak_person, self._streak = None, 0
