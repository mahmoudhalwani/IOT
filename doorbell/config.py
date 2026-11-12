"""Settings for the door unit. Every value can be overridden with an
environment variable named DOORBELL_<FIELD>, e.g. DOORBELL_MATCH_THRESHOLD=0.45."""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # Paths
    data_dir: Path = ROOT / "data"
    models_dir: Path = ROOT / "models"

    # Camera
    camera_index: int = 0
    frame_width: int = 640
    frame_height: int = 480

    # Detection
    det_score: float = 0.8          # YuNet confidence needed to call something a face
    min_face_px: int = 90           # face must be at least this wide (visitor close enough)
    edge_margin_px: int = 8         # face box must be this far inside the frame

    # Recognition (SFace cosine similarity; OpenCV's suggested minimum is 0.363)
    match_threshold: float = 0.42   # stricter than the default: target is zero stranger unlocks
    match_margin: float = 0.05      # best person must beat the runner-up by this much

    # Liveness (photo / screen rejection)
    live_threshold: float = 0.80    # mean "real" probability of the two MiniFASNet models
    live_frames_required: int = 5   # consecutive live frames of the same person before unlock
    spoof_frames_to_deny: int = 3   # spoof frames in a row before the visit is called an attack
    spoof_frames_total: int = 10    # ...or this many spoof frames in one visit, in a row or not
    gap_frames_allowed: int = 3     # frames with no usable face that do not restart the count
    unknown_frames_to_deny: int = 5 # live-but-unrecognised frames before "unknown"

    # Door
    relock_seconds: float = 5.0     # door locks again this long after unlocking
    visit_idle_seconds: float = 4.0 # motion but no face for this long: back to idle
    visit_max_seconds: float = 20.0 # give up on a visitor who never gives a clear view
    clear_max_seconds: float = 10.0 # longest wait for a visitor to leave before re-arming
    camera_retry_seconds: float = 3.0
    log_keep: int = 500             # newest entries kept in the access log

    # Photos and clips of visits
    clip_seconds: float = 5.0       # length of the clip kept for a refused visitor
    clip_fps: int = 15
    clip_jpeg_quality: int = 70
    photo_jpeg_quality: int = 85
    media_keep_days: float = 7.0    # photos and clips are deleted after this
    media_max_mb: float = 500.0     # oldest are deleted first if the folder grows past this

    # Hardware ("auto" = real pins on a Raspberry Pi, simulated anywhere else)
    hardware: str = "auto"          # "auto", "pi" or "sim"
    servo_pin: int = 18             # GPIO18 = physical pin 12
    servo_locked_angle: float = 0.0
    servo_unlocked_angle: float = 90.0
    servo_min_pulse_ms: float = 0.5
    servo_max_pulse_ms: float = 2.5
    servo_move_seconds: float = 0.7 # time allowed for the move before the servo is released
    motion_pin: int = 17            # GPIO17 = physical pin 11; -1 = no sensor, use the camera
    motion_active_high: bool = True # PIR modules: True. IR obstacle modules (FC-51): False

    # Website
    web_host: str = "127.0.0.1"     # only this machine; the HTTPS tunnel connects locally
    web_port: int = 8000
    web_dev: bool = False           # True = allow plain http cookies (laptop testing only)
    session_idle_hours: float = 24  # signed out after this long without use
    session_max_days: float = 14    # signed out this long after login, whatever happens
    login_fail_limit: int = 5       # wrong passwords before the account is paused
    login_pause_seconds: float = 60 # first pause; doubles each time, up to 15 minutes
    command_window_seconds: float = 30   # a signed door command is valid this long
    upload_max_mb: float = 8        # per enrolment photo

    # Enrolment
    max_templates_per_person: int = 5

    @property
    def db_path(self) -> Path:
        return self.data_dir / "doorbell.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "master.key"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        for f in fields(cls):
            raw = os.environ.get(f"DOORBELL_{f.name.upper()}")
            if raw is None:
                continue
            current = getattr(cfg, f.name)
            if isinstance(current, bool):
                value = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                value = Path(raw) if isinstance(current, Path) else type(current)(raw)
            setattr(cfg, f.name, value)
        return cfg
