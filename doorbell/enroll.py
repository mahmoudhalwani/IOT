"""Adding a person's face. Used by the command line now and by the website later."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Config
from .face import FaceEngine, Gallery
from .store import Store


class EnrollError(ValueError):
    pass


@dataclass(frozen=True)
class EnrollResult:
    template_id: int
    face_width: int


def template_from_image(engine: FaceEngine, image: np.ndarray) -> tuple[np.ndarray, int]:
    """Quality gate for an enrolment photo: exactly one clear, large-enough face."""
    faces = engine.detect(image)
    if not faces:
        raise EnrollError("no face found in the photo")
    if len(faces) > 1:
        raise EnrollError("more than one face in the photo; use a photo of one person")
    face = faces[0]
    if face.w < engine.cfg.min_face_px:
        raise EnrollError(f"face is too small ({face.w}px wide, need {engine.cfg.min_face_px}px)")
    return engine.embed(image, face), face.w


def add_face(engine: FaceEngine, gallery: Gallery, store: Store, cfg: Config,
             person_id: int, image: np.ndarray) -> EnrollResult:
    person = store.person(person_id)
    if person is None:
        raise EnrollError(f"no person with id {person_id}")
    if person.template_count >= cfg.max_templates_per_person:
        raise EnrollError(f"{person.name} already has {cfg.max_templates_per_person} photos")
    emb, width = template_from_image(engine, image)
    other, sim = gallery.best(emb)
    if other is not None and other != person_id and sim >= cfg.match_threshold:
        clash = store.person(other)
        raise EnrollError(f"this face already belongs to {clash.name if clash else other} "
                          f"(similarity {sim:.2f})")
    tid = store.add_template(person_id, emb)
    gallery.load(store.templates())
    return EnrollResult(tid, width)
