from pathlib import Path

import cv2
import numpy as np
import pytest

from doorbell.config import Config
from doorbell.crypto import Vault, load_or_create_key
from doorbell.face import FaceEngine, Gallery
from doorbell.store import Store

FIX = Path(__file__).parent / "fixtures"


def unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=128).astype(np.float32)
    return v / np.linalg.norm(v)


class StubIdentityEngine(FaceEngine):
    """Real detector and real liveness models; identity is set by the test, because
    the SFace model is downloaded separately (scripts/download_models.sh)."""

    def _load_recogniser(self):
        self.identity = unit(0)

    def embed(self, frame, face):
        return self.identity


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path)


@pytest.fixture
def store(cfg):
    return Store(cfg.db_path, Vault(load_or_create_key(cfg.key_path)))


@pytest.fixture(scope="session")
def engine():
    return StubIdentityEngine(Config())


@pytest.fixture
def gallery(cfg):
    return Gallery(cfg)


@pytest.fixture(scope="session")
def live():
    return cv2.imread(str(FIX / "live_1.jpg"))


@pytest.fixture(scope="session")
def spoofs():
    return [cv2.imread(str(FIX / n)) for n in ("spoof_1.jpg", "spoof_2.jpg")]
