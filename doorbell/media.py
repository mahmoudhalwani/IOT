"""Photos and short clips of visits, stored encrypted (MH-03, MH-11).

Every file is AES-256-GCM encrypted with the device key and can only be opened
through the website by a signed-in resident. Files are deleted after
`media_keep_days` (7 by default).

A clip is a sequence of JPEG frames (Motion-JPEG). That plays in any browser
without a video codec, and it can be encrypted in memory, so no unencrypted
video ever touches the disk.

Clip layout after decryption:  b"DBCL" | fps (1 byte) | { length (4 bytes, big-endian) | JPEG } ...
"""
from __future__ import annotations

import os
import re
import secrets
import struct
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .config import Config
from .crypto import Vault

MAGIC = b"DBCL"
_ID = re.compile(r"^[0-9a-f]{32}$")


def to_jpeg(frame: np.ndarray, quality: int) -> Optional[bytes]:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def pack_clip(frames: list[bytes], fps: int) -> bytes:
    return MAGIC + bytes([fps]) + b"".join(struct.pack(">I", len(f)) + f for f in frames)


def unpack_clip(blob: bytes) -> tuple[int, list[bytes]]:
    if blob[:4] != MAGIC:
        raise ValueError("not a clip")
    fps, pos, frames = blob[4], 5, []
    while pos < len(blob):
        (n,) = struct.unpack(">I", blob[pos:pos + 4])
        frames.append(blob[pos + 4:pos + 4 + n])
        pos += 4 + n
    return fps, frames


class ClipRecorder:
    """Collects frames at the clip frame rate while a visit is being judged."""

    def __init__(self, cfg: Config, clock=time.monotonic):
        self.cfg, self._clock = cfg, clock
        self.frames: list[bytes] = []
        self._last = -1e9
        self._max = int(cfg.clip_seconds * cfg.clip_fps)

    def add(self, frame: np.ndarray) -> None:
        now = self._clock()
        if now - self._last < 1 / self.cfg.clip_fps - 0.005:
            return
        jpeg = to_jpeg(frame, self.cfg.clip_jpeg_quality)
        if jpeg is None:
            return
        self._last = now
        self.frames.append(jpeg)
        if len(self.frames) > self._max:          # a long visit: keep the newest part
            del self.frames[0]

    @property
    def full(self) -> bool:
        return len(self.frames) >= self._max


class MediaStore:
    def __init__(self, cfg: Config, vault: Vault):
        self.cfg, self._vault = cfg, vault
        self.dir = Path(cfg.data_dir) / "media"
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(16)

    def save_photo(self, media_id: str, frame: np.ndarray) -> bool:
        jpeg = to_jpeg(frame, self.cfg.photo_jpeg_quality)
        return jpeg is not None and self._write(media_id, "photo", jpeg)

    def save_clip(self, media_id: str, frames: list[bytes]) -> bool:
        return bool(frames) and self._write(media_id, "clip", pack_clip(frames, self.cfg.clip_fps))

    def photo(self, media_id: str) -> Optional[bytes]:
        return self._read(media_id, "photo")

    def clip(self, media_id: str) -> Optional[bytes]:
        return self._read(media_id, "clip")

    def purge(self, now: Optional[float] = None) -> int:
        """Deletes files older than the keep time, then the oldest files while the
        folder is over its size limit. Returns how many were deleted."""
        now = time.time() if now is None else now
        files = sorted((p for p in self.dir.glob("*.enc")), key=lambda p: p.stat().st_mtime)
        cutoff = now - self.cfg.media_keep_days * 86400
        total = sum(p.stat().st_size for p in files)
        limit = self.cfg.media_max_mb * 1024 * 1024
        deleted = 0
        for p in files:
            size = p.stat().st_size
            if p.stat().st_mtime < cutoff or total > limit:
                p.unlink(missing_ok=True)
                total -= size
                deleted += 1
        return deleted

    # ---- internals --------------------------------------------------------
    def _path(self, media_id: str, kind: str) -> Path:
        if not _ID.match(media_id or ""):         # ids come from the database; never trust a path
            raise ValueError("bad media id")
        return self.dir / f"{media_id}.{kind}.enc"

    def _write(self, media_id: str, kind: str, data: bytes) -> bool:
        path = self._path(media_id, kind)
        tmp = path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(self._vault.seal(data, f"{kind}:{media_id}".encode()))
            os.replace(tmp, path)
            return True
        except OSError:
            tmp.unlink(missing_ok=True)
            return False

    def _read(self, media_id: str, kind: str) -> Optional[bytes]:
        try:
            path = self._path(media_id, kind)
            return self._vault.open(path.read_bytes(), f"{kind}:{media_id}".encode())
        except (OSError, ValueError):
            return None
        except Exception:                          # tampered or wrong-key file
            return None
