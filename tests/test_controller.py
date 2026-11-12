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


# ---- photos and clips ------------------------------------------------------
import cv2
from doorbell.media import MediaStore, unpack_clip


@pytest.fixture
def media_rig(rig, store, cfg):
    def make(frames, **kw):
        ctl, lock, clock, said, cam = rig(frames, **kw)
        ctl.media = MediaStore(cfg, store.vault)
        return ctl, ctl.media, clock, said, cam
    return make


def is_jpeg(data):
    return data[:2] == b"\xff\xd8" and cv2.imdecode(np.frombuffer(data, np.uint8), 1) is not None


def test_resident_visit_keeps_a_photo_but_no_clip(media_rig, store, live):
    ctl, media, *_ = media_rig([live] * 8)
    ctl.cycle()
    (e,) = store.events()
    assert e.result == "granted" and e.photo and e.clip is None and e.clip_reason
    assert is_jpeg(media.photo(e.photo))


def test_stranger_and_attack_get_a_photo_and_a_five_second_clip(media_rig, store, cfg, live, spoofs):
    for frame, result in ((live, "denied_unknown"), (spoofs[1], "denied_spoof")):
        ctl, media, *_ = media_rig([frame] * 200, enrolled=False)
        ctl.cycle()
        e = store.events()[0]
        assert e.result == result and e.photo and e.clip
        assert is_jpeg(media.photo(e.photo))
        fps, frames = unpack_clip(media.clip(e.clip))
        assert fps == cfg.clip_fps and len(frames) >= cfg.clip_seconds * cfg.clip_fps
        assert all(is_jpeg(f) for f in frames[:3] + frames[-3:])


def test_media_files_are_encrypted_and_private(media_rig, store, live):
    ctl, media, *_ = media_rig([live] * 200, enrolled=False)
    ctl.cycle()
    files = list(media.dir.glob("*.enc"))
    assert len(files) == 2
    for f in files:
        raw = f.read_bytes()
        assert b"JFIF" not in raw and b"\xff\xd8\xff" not in raw[:16] and b"DBCL" not in raw[:16]
        assert oct(f.stat().st_mode & 0o777) == "0o600"
    e = store.events()[0]
    assert media.clip(e.photo) is None                    # a photo id does not open as a clip
    files[0].write_bytes(files[0].read_bytes()[:-1] + b"\x00")
    assert media.photo(e.photo) is None or media.clip(e.clip) is None   # tampering is detected


def test_clip_survives_camera_failure_and_log_says_so_when_empty(media_rig, store, live):
    ctl, media, clock, said, cam = media_rig([live] * 8 + [None], enrolled=False)
    ctl.cycle()
    e = store.events()[0]
    assert e.result == "denied_unknown" and len(unpack_clip(media.clip(e.clip))[1]) >= 1


def test_old_media_is_deleted_after_seven_days(media_rig, store, cfg, live):
    import os, time as real_time
    ctl, media, *_ = media_rig([live] * 200, enrolled=False)
    ctl.cycle()
    e = store.events()[0]
    old = real_time.time() - 8 * 86400
    os.utime(media._path(e.clip, "clip"), (old, old))
    assert media.purge() == 1
    assert media.clip(e.clip) is None and media.photo(e.photo) is not None
    assert media.purge(real_time.time() + 8 * 86400) == 1 and media.photo(e.photo) is None
    with pytest.raises(ValueError):
        media._path("../../etc/passwd", "photo")


def test_database_from_an_older_version_is_upgraded(cfg):
    import sqlite3
    from doorbell.crypto import Vault, load_or_create_key
    from doorbell.store import Store
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(cfg.db_path)
    db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, method TEXT NOT NULL, "
               "result TEXT NOT NULL, person TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', clip TEXT, clip_reason TEXT)")
    db.execute("INSERT INTO events (at, method, result, person) VALUES ('2026-11-12T10:00:00', 'face', 'granted', 'Sara')")
    db.commit(); db.close()
    store = Store(cfg.db_path, Vault(load_or_create_key(cfg.key_path)))
    store.add_event("face", "denied_unknown", photo="ab" * 16)
    assert [e.photo for e in store.events()] == ["ab" * 16, None]
