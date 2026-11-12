from datetime import datetime, time

import numpy as np
import pytest

from conftest import unit
from doorbell.access import AccessRule
from doorbell.decision import Outcome, Visit
from doorbell.enroll import EnrollError, add_face


def enrol(store, gallery, vec, name="Sara", **kw):
    pid = store.add_person(name, consent=True, **kw)
    store.add_template(pid, vec)
    gallery.load(store.templates())
    return pid


def run(visit, frame, n, now=None):
    return [visit.update(frame, now) for _ in range(n)]


def test_liveness_models_on_sample_photos(engine, cfg, live, spoofs):
    assert engine.liveness(live, engine.detect(live)[0]).passed(cfg.live_threshold)
    for img in spoofs:
        assert not engine.liveness(img, engine.detect(img)[0]).passed(cfg.live_threshold)


def test_resident_granted_only_after_enough_live_frames(engine, gallery, store, cfg, live):
    pid = enrol(store, gallery, engine.identity)
    out = run(Visit(engine, gallery, store, cfg), live, cfg.live_frames_required)
    assert [d.outcome for d in out[:-1]] == [Outcome.CHECKING] * (cfg.live_frames_required - 1)
    assert out[-1].outcome is Outcome.GRANTED and out[-1].person_id == pid


def test_photo_of_resident_never_unlocks(engine, gallery, store, cfg, spoofs):
    enrol(store, gallery, engine.identity)          # the photo "is" the resident
    for img in spoofs:
        out = run(Visit(engine, gallery, store, cfg), img, 20)
        assert Outcome.GRANTED not in [d.outcome for d in out]
        assert out[-1].outcome is Outcome.SPOOF and out[-1].person_id is None


def test_spoof_frame_resets_the_run(engine, gallery, store, cfg, live, spoofs):
    enrol(store, gallery, engine.identity)
    visit = Visit(engine, gallery, store, cfg)
    run(visit, live, cfg.live_frames_required - 1)
    assert visit.update(spoofs[1]).outcome is Outcome.CHECKING
    out = run(visit, live, cfg.live_frames_required)
    assert [d.outcome for d in out].index(Outcome.GRANTED) == cfg.live_frames_required - 1


def test_stranger_is_unknown(engine, gallery, store, cfg, live):
    enrol(store, gallery, unit(42))                  # somebody else is enrolled
    out = run(Visit(engine, gallery, store, cfg), live, cfg.unknown_frames_to_deny)
    assert out[-1].outcome is Outcome.UNKNOWN and out[-1].person_id is None


def test_empty_database_never_unlocks(engine, gallery, store, cfg, live):
    out = run(Visit(engine, gallery, store, cfg), live, 10)
    assert Outcome.GRANTED not in [d.outcome for d in out]


def test_lookalike_between_two_people_is_rejected(engine, gallery, store, cfg, live):
    near = engine.identity + 0.02 * unit(7)
    enrol(store, gallery, engine.identity, "A")
    enrol(store, gallery, near / np.linalg.norm(near), "B")
    out = run(Visit(engine, gallery, store, cfg), live, 10)
    assert Outcome.GRANTED not in [d.outcome for d in out]


def test_guest_outside_allowed_hours(engine, gallery, store, cfg, live):
    enrol(store, gallery, engine.identity, "Cleaner", role="guest",
          rule=AccessRule(start=time(9), end=time(12)))
    n = cfg.live_frames_required
    inside = run(Visit(engine, gallery, store, cfg), live, n, datetime(2026, 11, 9, 10))
    outside = run(Visit(engine, gallery, store, cfg), live, n, datetime(2026, 11, 9, 20))
    assert inside[-1].outcome is Outcome.GRANTED
    assert outside[-1].outcome is Outcome.NOT_ALLOWED and outside[-1].name == "Cleaner"


def test_removed_person_stops_unlocking(engine, gallery, store, cfg, live):
    pid = enrol(store, gallery, engine.identity)
    store.remove_person(pid)
    gallery.load(store.templates())
    out = run(Visit(engine, gallery, store, cfg), live, 10)
    assert Outcome.GRANTED not in [d.outcome for d in out]


def test_small_or_missing_face(engine, gallery, store, cfg, live):
    enrol(store, gallery, engine.identity)
    visit = Visit(engine, gallery, store, cfg)
    assert visit.update(np.zeros((480, 640, 3), np.uint8)).outcome is Outcome.NO_FACE
    tiny = np.zeros((480, 640, 3), np.uint8)
    tiny[:160, :120] = __import__("cv2").resize(live, (120, 160))
    assert visit.update(tiny).outcome in (Outcome.POSITION, Outcome.NO_FACE)


def test_enrolment_rules(engine, gallery, store, cfg, live):
    a = store.add_person("A", consent=True)
    add_face(engine, gallery, store, cfg, a, live)
    b = store.add_person("B", consent=True)
    with pytest.raises(EnrollError, match="already belongs to A"):
        add_face(engine, gallery, store, cfg, b, live)
    with pytest.raises(EnrollError, match="no face"):
        add_face(engine, gallery, store, cfg, a, np.zeros((480, 640, 3), np.uint8))
    for _ in range(cfg.max_templates_per_person - 1):
        add_face(engine, gallery, store, cfg, a, live)
    with pytest.raises(EnrollError, match="already has"):
        add_face(engine, gallery, store, cfg, a, live)


def test_brief_loss_of_face_does_not_restart_the_count(engine, gallery, store, cfg, live):
    enrol(store, gallery, engine.identity)
    blank = np.zeros((480, 640, 3), np.uint8)
    n = cfg.live_frames_required
    visit = Visit(engine, gallery, store, cfg)
    run(visit, live, n - 1)
    run(visit, blank, cfg.gap_frames_allowed)
    assert visit.update(live).outcome is Outcome.GRANTED
    visit = Visit(engine, gallery, store, cfg)           # a longer absence does restart it
    run(visit, live, n - 1)
    run(visit, blank, cfg.gap_frames_allowed + 1)
    assert visit.update(live).outcome is Outcome.CHECKING


def test_occasional_doubtful_frames_do_not_brand_a_live_person(engine, gallery, store, cfg,
                                                               live, monkeypatch):
    from doorbell.face import Liveness
    enrol(store, gallery, engine.identity)
    scores = iter([0.9, 0.6, 0.9, 0.9, 0.6, 0.9, 0.9, 0.9, 0.9, 0.9])
    monkeypatch.setattr(engine, "liveness", lambda f, face: Liveness(next(scores), True))
    out = [d.outcome for d in run(Visit(engine, gallery, store, cfg), live, 10)]
    assert Outcome.SPOOF not in out and out[-1] is Outcome.GRANTED and out.count(Outcome.GRANTED) == 1


def test_mostly_fake_visit_is_spoof_even_with_lucky_frames(engine, gallery, store, cfg,
                                                           live, monkeypatch):
    from doorbell.face import Liveness
    enrol(store, gallery, engine.identity)
    pattern = [0.3, 0.3, 0.9, 0.9] * 10                  # never 5 good frames in a row
    scores = iter(pattern)
    monkeypatch.setattr(engine, "liveness", lambda f, face: Liveness(next(scores), True))
    out = [d.outcome for d in run(Visit(engine, gallery, store, cfg), live, len(pattern))]
    assert Outcome.GRANTED not in out and Outcome.SPOOF in out
