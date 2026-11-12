import numpy as np
import pytest

from doorbell.controller import Controller
from doorbell.decision import Outcome
from doorbell.hardware import SimLock

BLANK = np.zeros((480, 640, 3), np.uint8)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeCamera:
    """Plays a list of frames (None = camera failure), then blank frames for ever.
    Every frame takes 50 ms of fake time."""

    def __init__(self, clock, frames, opens=True):
        self.clock, self.frames, self.opens, self.open_calls = clock, list(frames), opens, 0

    def open(self):
        self.open_calls += 1
        return self.opens

    def read(self):
        self.clock.t += 0.05
        return self.frames.pop(0) if self.frames else BLANK

    def skip(self):
        self.clock.t += 0.05

    def close(self):
        pass


class FakeMotion:
    def __init__(self, active=True):
        self.active = active

    def detected(self):
        return self.active


@pytest.fixture
def rig(engine, gallery, store, cfg):
    def make(frames, motion=None, enrolled=True, **kw):
        if enrolled:
            pid = store.add_person("Sara", consent=True, **kw)
            store.add_template(pid, engine.identity)
        clock, said = Clock(), []
        lock = SimLock(said.append)
        cam = FakeCamera(clock, frames)
        ctl = Controller(cfg, engine, gallery, store, lock, cam, motion,
                         sleep=clock.sleep, clock=clock.now, say=said.append)
        return ctl, lock, clock, said, cam
    return make


def lock_moves(said):
    return [m for m in said if m.startswith("[lock]")]


def test_resident_unlocks_then_relocks_after_five_seconds(rig, store, cfg, live):
    ctl, lock, clock, said, _ = rig([live] * 8)
    d = ctl.cycle()
    assert d.outcome is Outcome.GRANTED
    assert lock_moves(said) == ["[lock] UNLOCKED", "[lock] LOCKED"] and lock.locked is True
    assert clock.t >= cfg.relock_seconds
    (e,) = store.events()
    assert (e.method, e.result, e.person) == ("face", "granted", "Sara") and e.clip_reason


def test_stranger_and_photo_stay_locked_and_are_logged(rig, store, live, spoofs):
    ctl, lock, _, said, _ = rig([live] * 8, enrolled=False)
    assert ctl.cycle().outcome is Outcome.UNKNOWN
    ctl.camera.frames = [spoofs[1]] * 8
    assert ctl.cycle().outcome is Outcome.SPOOF
    assert lock_moves(said) == []
    assert [e.result for e in store.events()] == ["denied_spoof", "denied_unknown"]


def test_same_person_is_not_judged_twice_while_standing_there(rig, store, live):
    ctl, _, _, said, cam = rig([live] * 40)
    ctl.cycle()
    assert len(store.events()) == 1 and len(lock_moves(said)) == 2
    assert len(cam.frames) < 32          # it kept watching (waiting) without deciding again


def test_motion_without_a_face_is_not_an_entry_attempt(rig, store):
    ctl, _, _, said, _ = rig([], motion=FakeMotion(True))
    assert ctl.cycle() is None
    assert store.events() == [] and lock_moves(said) == []


def test_no_motion_means_camera_is_not_analysed(rig, live):
    ctl, _, _, _, cam = rig([live] * 8, motion=FakeMotion(False))
    assert ctl.cycle() is None and len(cam.frames) == 8


def test_camera_failure_keeps_door_locked_logs_once_and_recovers(rig, store, live):
    ctl, _, _, said, cam = rig([live, live, None])
    assert ctl.cycle() is None                       # failure in the middle of a visit
    cam.opens = False
    for _ in range(3):
        assert ctl.cycle() is None                   # retries, still broken
    assert [e.result for e in store.events()] == ["fault"] and lock_moves(said) == []
    cam.opens, cam.frames = True, [live] * 10
    ctl.cycle()                                      # reopens
    assert ctl.cycle().outcome is Outcome.GRANTED


def test_door_is_locked_on_start_and_on_stop_even_mid_unlock(rig, live):
    ctl, lock, clock, said, _ = rig([live] * 8)

    def interrupted_sleep(s):
        if s == ctl.cfg.relock_seconds:
            raise KeyboardInterrupt                  # program stopped while the door is open
        clock.t += s
    ctl._sleep = interrupted_sleep
    with pytest.raises(KeyboardInterrupt):
        ctl.run()
    assert lock_moves(said)[0] == "[lock] LOCKED"    # first action at start
    assert lock_moves(said)[-1] == "[lock] LOCKED" and lock.locked is True


def test_log_keeps_only_the_newest_entries(store):
    store._log_keep = 5
    for i in range(12):
        store.add_event("face", "denied_unknown", detail=str(i))
    assert [e.detail for e in store.events()] == ["11", "10", "9", "8", "7"]
    assert not hasattr(store, "delete_event") and not hasattr(store, "update_event")
