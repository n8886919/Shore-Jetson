"""Tracking-only Pelco-D writer with STOP delivery and a host producer lease.

No receive-side watchdog, position feedback or mechanical limit is assumed.
The runtime hands the exclusive port between tracking and manual control.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .pelco_d import encode_velocity
from .simple import TARGET_MAX_AGE_SECONDS, _SerialWriter, _Writer


@dataclass(frozen=True)
class PelcoHealth:
    ready: bool
    armed: bool
    last_error: str | None


class PelcoServoClient:
    protocol = "pelco_d"

    def __init__(self, writer: _Writer, *, address=1, send_hz=20,
                 max_pan_speed=8, max_tilt_speed=8, clock=time.monotonic):
        self._stop_frame = encode_velocity(address)
        if type(send_hz) is not int or not 10 <= send_hz <= 50:
            raise ValueError("send_hz must be in [10, 50]")
        if any(type(v) is not int or not 1 <= v <= 63 for v in (max_pan_speed, max_tilt_speed)):
            raise ValueError("Pelco speed limits must be in [1, 63]")
        self.writer, self.address, self.clock = writer, address, clock
        self.max_pan_speed, self.max_tilt_speed = max_pan_speed, max_tilt_speed
        self.period = 1 / send_hz
        self.on_error = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._opened = self._ready = self._armed = False
        self._run_id = self._target = self._updated_at = self._last_frame = self._last_error = None

    def start(self):
        with self._lock:
            if self._opened or self._last_error:
                raise RuntimeError("Create a new Pelco client to restart")
            self.writer.open()
            self._opened = self._ready = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="pelco-writer", daemon=True)
            self._thread.start()
            # Opening the port does not send motion, STOP or an assumed home.

    def arm(self, *, run_id: int):
        if type(run_id) is not int or run_id < 1:
            raise ValueError("A current tracking run is required")
        with self._lock:
            if not self._ready or self._last_error:
                raise RuntimeError("Pelco transport is not ready")
            if self._armed:
                raise RuntimeError("Pelco client is already armed")
            self._armed, self._run_id = True, run_id
            self._target = self._updated_at = None

    def set_velocity(self, pan: int, tilt: int, *, run_id: int) -> bool:
        frame = encode_velocity(self.address, pan, tilt)
        if abs(pan) > self.max_pan_speed or abs(tilt) > self.max_tilt_speed:
            raise ValueError("Pelco target exceeds configured speed limits")
        reason = None
        with self._lock:
            # A delayed old control tick cannot revive motion after STOP/re-ARM.
            if (not self._ready or not self._armed or type(run_id) is not int
                    or run_id != self._run_id):
                return False
            self._target, self._updated_at = (pan, tilt), self.clock()
            if frame == self._stop_frame and self._last_frame != frame:
                try:
                    self.writer.write(frame)
                    self._last_frame = frame
                except Exception:
                    reason = self._fail_locked("transport_write_failed")
        if reason:
            self._notify(reason)
            raise OSError("Pelco STOP write failed")
        return True

    def stop_motion(self):
        self._clear_and_stop(disarm=True)

    def disable_tilt(self):
        # Clear both cached axes and send full STOP; the next fresh tick may pan.
        self._clear_and_stop(disarm=False)

    def _clear_and_stop(self, *, disarm):
        reason = None
        with self._lock:
            if disarm:
                self._armed, self._run_id = False, None
            self._target = self._updated_at = None
            if self._opened:
                try:
                    self.writer.write(self._stop_frame)
                    self._last_frame = self._stop_frame
                except Exception:
                    reason = self._fail_locked("transport_write_failed")
        if reason:
            self._notify(reason)
            raise OSError("Pelco STOP write failed")

    def snapshot(self):
        with self._lock:
            return PelcoHealth(self._ready, self._armed, self._last_error)

    def _fail_locked(self, reason):
        self._ready = self._armed = False
        self._run_id = self._target = self._updated_at = None
        self._last_error = reason
        self._stop.set()
        # A partial moving frame might already have reached the head.
        try:
            self.writer.write(self._stop_frame)
            self._last_frame = self._stop_frame
        except Exception:
            self._last_frame = None
        return reason

    def _notify(self, reason):
        # Never call status/controller callbacks while holding the writer lock.
        if self.on_error is not None:
            self.on_error(reason)

    def _send_target(self):
        reason = None
        with self._lock:
            if not self._ready or not self._armed or self._target is None:
                return
            age = self.clock() - self._updated_at
            if not 0 <= age < TARGET_MAX_AGE_SECONDS:
                reason = self._fail_locked("target_expired")
            else:
                frame = encode_velocity(self.address, *self._target)
                if frame != self._stop_frame or self._last_frame != frame:
                    try:
                        self.writer.write(frame)
                        self._last_frame = frame
                    except Exception:
                        reason = self._fail_locked("transport_write_failed")
        if reason:
            self._notify(reason)

    def _run(self):
        while not self._stop.wait(self.period):
            self._send_target()

    def close(self, *, timeout=1.0):
        stop_ok = True
        try:
            self.stop_motion()
        except OSError:
            stop_ok = False
        with self._lock:
            self._ready = False
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self.writer.close()
                self._opened = False
                self._thread = None
        return stopped and stop_ok


def pelco_serial_client(config, *, serial_factory=None):
    return PelcoServoClient(
        _SerialWriter(device=config.serial_device, baud=config.serial_baud,
                      protocol="pelco_d", serial_factory=serial_factory),
        address=config.pelco_address, send_hz=config.send_hz,
        max_pan_speed=config.pelco_pan_speed, max_tilt_speed=config.pelco_tilt_speed)
