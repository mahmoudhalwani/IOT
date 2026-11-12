"""Face pipeline: YuNet (detect) -> MiniFASNet x2 (liveness) -> SFace (identity).

Everything runs on the device through OpenCV; no image leaves the Pi.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import Config

YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"
# (file, crop scale): each model looks at a different amount of context around the
# face. The wider crop is what catches phone bezels and paper edges.
LIVENESS = [("minifasnet_v2_2.7.onnx", 2.7), ("minifasnet_v1se_4.0.onnx", 4.0)]
REAL = 1  # class index of "real face" in the MiniFASNet output


@dataclass(frozen=True)
class Face:
    x: int
    y: int
    w: int
    h: int
    score: float
    row: np.ndarray     # raw YuNet row (box + 5 landmarks), needed for alignment


@dataclass(frozen=True)
class Liveness:
    p_real: float       # mean "real" probability across the models
    agree: bool         # every model's top class is "real"

    def passed(self, threshold: float) -> bool:
        return self.agree and self.p_real >= threshold


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max())
    return e / e.sum()


class FaceEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._m = Path(cfg.models_dir)
        self._det = cv2.FaceDetectorYN.create(self._model(YUNET), "", (320, 320), cfg.det_score, 0.3, 50)
        self._det_size = (0, 0)
        self._live = [(cv2.dnn.readNetFromONNX(self._model(f)), s) for f, s in LIVENESS]
        self._load_recogniser()

    def _model(self, name: str) -> str:
        path = self._m / name
        if not path.exists():
            raise FileNotFoundError(f"model file missing: {path}  (run scripts/download_models.sh)")
        return str(path)

    def _load_recogniser(self) -> None:
        self._rec = cv2.FaceRecognizerSF.create(self._model(SFACE), "")

    # ---- detection --------------------------------------------------------
    def detect(self, frame: np.ndarray) -> list[Face]:
        """All faces in the frame, largest (closest to the door) first."""
        h, w = frame.shape[:2]
        if self._det_size != (w, h):
            self._det.setInputSize((w, h))
            self._det_size = (w, h)
        _, rows = self._det.detect(frame)
        if rows is None:
            return []
        faces = [Face(int(r[0]), int(r[1]), int(r[2]), int(r[3]), float(r[-1]), r) for r in rows]
        return sorted(faces, key=lambda f: f.w * f.h, reverse=True)

    def placement_problem(self, frame: np.ndarray, face: Face) -> str:
        """Empty string if the face is big enough and fully inside the picture (the
        liveness crops need the surroundings they were trained on), else what to fix."""
        h, w = frame.shape[:2]
        m = self.cfg.edge_margin_px
        if face.w < self.cfg.min_face_px:
            return "step closer"
        if face.x < m or face.y < m or face.x + face.w > w - m or face.y + face.h > h - m:
            return "move to the middle of the picture"
        return ""

    def well_placed(self, frame: np.ndarray, face: Face) -> bool:
        return not self.placement_problem(frame, face)

    # ---- liveness ---------------------------------------------------------
    def liveness(self, frame: np.ndarray, face: Face) -> Liveness:
        probs = []
        for net, scale in self._live:
            crop = self._context_crop(frame, face, scale).astype(np.float32)
            net.setInput(crop.transpose(2, 0, 1)[None])
            probs.append(_softmax(net.forward()[0]))
        return Liveness(p_real=float(np.mean([p[REAL] for p in probs])),
                        agree=all(int(np.argmax(p)) == REAL for p in probs))

    @staticmethod
    def _context_crop(frame: np.ndarray, face: Face, scale: float) -> np.ndarray:
        """Crop `scale` times the face box around its centre, as in the original
        Silent-Face-Anti-Spoofing preprocessing, resized to 80x80."""
        fh, fw = frame.shape[:2]
        scale = min((fh - 1) / face.h, (fw - 1) / face.w, scale)
        nw, nh = face.w * scale, face.h * scale
        cx, cy = face.x + face.w / 2, face.y + face.h / 2
        x1, y1 = max(0, int(cx - nw / 2)), max(0, int(cy - nh / 2))
        x2, y2 = min(fw - 1, int(cx + nw / 2)), min(fh - 1, int(cy + nh / 2))
        return cv2.resize(frame[y1:y2 + 1, x1:x2 + 1], (80, 80))

    # ---- identity ---------------------------------------------------------
    def embed(self, frame: np.ndarray, face: Face) -> np.ndarray:
        """128-number face template, length-normalised so a dot product is the
        cosine similarity."""
        aligned = self._rec.alignCrop(frame, face.row)
        vec = self._rec.feature(aligned).ravel().astype(np.float32)
        return vec / (np.linalg.norm(vec) + 1e-9)


class Gallery:
    """In-memory copy of the enrolled templates for fast matching."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ids = np.zeros(0, dtype=np.int64)
        self._vecs = np.zeros((0, 128), dtype=np.float32)

    def load(self, templates: list[tuple[int, np.ndarray]]) -> None:
        self._ids = np.array([pid for pid, _ in templates], dtype=np.int64)
        self._vecs = (np.stack([v for _, v in templates]).astype(np.float32)
                      if templates else np.zeros((0, 128), dtype=np.float32))

    def best(self, emb: np.ndarray) -> tuple[int | None, float]:
        """Closest person and their similarity, with no threshold applied."""
        if not len(self._ids):
            return None, 0.0
        sims = self._vecs @ emb
        i = int(np.argmax(sims))
        return int(self._ids[i]), float(sims[i])

    def match(self, emb: np.ndarray) -> tuple[int | None, float]:
        """Person id if the face clearly belongs to one enrolled person, else None.
        'Clearly' = above the threshold and ahead of every other person by a margin."""
        pid, sim = self.best(emb)
        if pid is None or sim < self.cfg.match_threshold:
            return None, sim
        others = self._vecs[self._ids != pid]
        if len(others) and sim - float((others @ emb).max()) < self.cfg.match_margin:
            return None, sim
        return pid, sim
