"""Things the software touches in the real world: the lock servo, the motion
sensor and the camera. On a computer without GPIO pins (a laptop) a simulated
lock is used so the whole system can be tried without the Pi."""
from __future__ import annotations

import time
from typing import Optional

import cv2
import numpy as np

from .config import Config


# ---- lock -------------------------------------------------------------------
class SimLock:
    """Stands in for the servo: prints what the real lock would do."""

    def __init__(self, say=print):
        self.locked: Optional[bool] = None
        self._say = say

    def lock(self) -> None:
        self.locked = True
        self._say("[lock] LOCKED")

    def unlock(self) -> None:
        self.locked = False
        self._say("[lock] UNLOCKED")

    def close(self) -> None:
        pass


class ServoLock:
    """Hobby servo on a GPIO pin. The servo is driven only while it moves and is
    then released: it does not buzz, draws no holding current, cannot stall for
    long, and the thumb-turn stays free for the key and the inside exit (MH-07)."""

    def __init__(self, cfg: Config):
        from gpiozero import AngularServo
        self.cfg = cfg
        self.locked: Optional[bool] = None
        self._servo = AngularServo(
            cfg.servo_pin, initial_angle=None, min_angle=0, max_angle=180,
            min_pulse_width=cfg.servo_min_pulse_ms / 1000,
            max_pulse_width=cfg.servo_max_pulse_ms / 1000)

    def move_to(self, angle: float) -> None:
        self._servo.angle = max(0.0, min(180.0, angle))
        time.sleep(self.cfg.servo_move_seconds)
        self._servo.detach()

    def lock(self) -> None:
        self.move_to(self.cfg.servo_locked_angle)
        self.locked = True

    def unlock(self) -> None:
        self.move_to(self.cfg.servo_unlocked_angle)
        self.locked = False

    def close(self) -> None:
        self._servo.close()


# ---- motion sensor ----------------------------------------------------------
class GpioMotion:
    """PIR or IR proximity module with a digital output pin."""

    def __init__(self, cfg: Config):
        from gpiozero import DigitalInputDevice
        self._pin = DigitalInputDevice(cfg.motion_pin, pull_up=None,
                                       active_state=cfg.motion_active_high)

    def detected(self) -> bool:
        return bool(self._pin.is_active)

    def close(self) -> None:
        self._pin.close()


# ---- camera -----------------------------------------------------------------
class Camera:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cap: Optional[cv2.VideoCapture] = None

    def open(self) -> bool:
        self.close()
        cap = cv2.VideoCapture(self.cfg.camera_index)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.frame_height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            cap.release()
            return False
        for _ in range(20):             # exposure needs a moment to settle
            cap.read()
        self._cap = cap
        return True

    def read(self) -> Optional[np.ndarray]:
        """Newest frame, or None if the camera has failed."""
        if self._cap is None:
            return None
        ok, frame = self._cap.read()
        return frame if ok else None

    def skip(self) -> None:
        """Throw one frame away without decoding it, so the next read() is fresh."""
        if self._cap is not None:
            self._cap.grab()

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# ---- choosing real or simulated ---------------------------------------------
def on_a_pi() -> bool:
    try:
        with open("/proc/device-tree/model") as fh:
            return "Raspberry Pi" in fh.read()
    except OSError:
        return False


def build(cfg: Config, say=print):
    """-> (lock, motion sensor or None). hardware = "pi", "sim" or "auto"."""
    real = cfg.hardware == "pi" or (cfg.hardware == "auto" and on_a_pi())
    if not real:
        say("[hardware] no GPIO here: simulated lock, camera used as the motion trigger")
        return SimLock(say), None
    lock = ServoLock(cfg)
    motion = GpioMotion(cfg) if cfg.motion_pin >= 0 else None
    return lock, motion
