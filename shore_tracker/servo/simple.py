from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .port_guard import require_control_port
from .pulse_controller import encode_position


# Host producer deadline; the controller has no established receive-side lease.
TARGET_MAX_AGE_SECONDS = 0.250


@dataclass(frozen=True, slots=True)
class ServoHealth:
    ready: bool
    armed: bool
    target_degrees: float | None
    last_error: str | None


class _Writer(Protocol):
    def open(self) -> None: ...
    def write(self, payload: bytes) -> None: ...
    def close(self) -> None: ...


def encode_command(command: str, degrees: float | None = None) -> bytes:
    if command != "SET":
        raise ValueError("UART accepts only SET")
    if degrees is None or not math.isfinite(degrees):
        raise ValueError("SET angle must be finite")
    if not 0 <= degrees <= 180:
        raise ValueError("SET angle must be in [0, 180]")
    return f"SET {round(degrees * 1000)}\n".encode("ascii")


class _SerialWriter:
    def __init__(
        self,
        *,
        device: str,
        baud: int,
        protocol: str = "shore_spotter",
        serial_factory: Callable[..., object] | None = None,
    ) -> None:
        self.device = device
        expected = {"shore_spotter": 115200, "m0_pulse": 9600, "pelco_d": 9600}.get(protocol)
        if expected is None or baud != expected:
            raise ValueError("UART baud does not match the selected protocol")
        self.baud = baud
        self.serial_factory = serial_factory
        self.port: object | None = None

    def open(self) -> None:
        require_control_port(self.device)
        factory = self.serial_factory
        if factory is None:
            import serial  # type: ignore[import-not-found]

            factory = serial.Serial
        self.port = factory(
            port=self.device,
            baudrate=self.baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0,
            write_timeout=0.1,
            exclusive=True,
        )

    def write(self, payload: bytes) -> None:
        if self.port is None:
            raise RuntimeError("serial writer is not open")
        written = self.port.write(payload)  # type: ignore[attr-defined]
        if written != len(payload):
            raise OSError("short serial write")

    def is_connected(self) -> bool:
        if self.port is None:
            return False
        try:
            opened = os.fstat(self.port.fileno())  # type: ignore[attr-defined]
            current = os.stat(self.device)
            return ((opened.st_dev, opened.st_ino, opened.st_rdev)
                    == (current.st_dev, current.st_ino, current.st_rdev))
        except (OSError, ValueError):
            return False

    def close(self) -> None:
        if self.port is not None:
            self.port.close()  # type: ignore[attr-defined]
            self.port = None


class SimpleServoClient:
    """One-way UART with a local send gate and periodic SET refresh.

    Only the worker writes; producers replace its pending target. ARM/STOP are
    local operations, never wire commands. A fresh target is required after
    each arm. Readiness refers to the host port, not the board or shaft.
    The independent control loop must keep producing targets. A stalled producer must
    not be hidden by refreshing an old SET indefinitely. Transport failures or
    expired targets latch and require a new client/service.
    """

    def __init__(
        self,
        writer: _Writer,
        *,
        angle_min_deg: float,
        angle_max_deg: float,
        send_hz: int = 50,
        protocol: str = "shore_spotter",
        clock: Callable[[], float] = time.monotonic,
        tilt_min_deg: float = 85.0,
        tilt_max_deg: float = 95.0,
        pan_channel: int = 0,
        tilt_channel: int = 1,
    ) -> None:
        maximum = {"shore_spotter": 180, "m0_pulse": 180}.get(protocol)
        if maximum is None or not 0 <= angle_min_deg < angle_max_deg <= maximum:
            raise ValueError("Servo angle range is invalid")
        if not 10 <= send_hz <= 50:
            raise ValueError("Servo send_hz must be in [10, 50]")
        self.writer = writer
        self.protocol = protocol
        self.angle_min_deg = angle_min_deg
        self.angle_max_deg = angle_max_deg
        if not 0 <= tilt_min_deg < tilt_max_deg <= 180:
            raise ValueError("Servo tilt range is invalid")
        self.tilt_min_deg = tilt_min_deg
        self.tilt_max_deg = tilt_max_deg
        if (type(pan_channel) is not int or type(tilt_channel) is not int
                or {pan_channel, tilt_channel} != {0, 1}):
            raise ValueError("Pan/tilt channels must be distinct 0/1 channels")
        self.pan_channel = pan_channel
        self.tilt_channel = tilt_channel
        self._tilt_target: float | None = None
        self.period = 1.0 / send_hz
        self.clock = clock
        self.on_error: Callable[[str], None] | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = False
        self._armed = False
        self._target: float | None = None
        self._target_updated_at: float | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._last_error is not None:
                raise RuntimeError("Servo transport failed; restart the service")
            if self._thread is not None:
                return
            self.writer.open()
            self._armed = False
            self._target = None
            self._tilt_target = None
            self._target_updated_at = None
            self._ready = True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="servo-text-writer", daemon=True
            )
            self._thread.start()

    def arm(self) -> None:
        with self._lock:
            self._ensure_ready()
            if not self._armed:
                self._target = None
                self._tilt_target = None
                self._target_updated_at = None
            self._armed = True

    def set_target_degrees(self, degrees: float, *, tilt_degrees: float | None = None) -> None:
        produced_at = self.clock()
        if not math.isfinite(degrees):
            raise ValueError("Servo target must be finite")
        if not self.angle_min_deg <= degrees <= self.angle_max_deg:
            raise ValueError("Servo target is outside configured limits")
        if tilt_degrees is not None:
            if self.protocol != "m0_pulse":
                raise ValueError("Tilt requires the two-channel pulse controller")
            if not math.isfinite(tilt_degrees) or not self.tilt_min_deg <= tilt_degrees <= self.tilt_max_deg:
                raise ValueError("Tilt target is outside configured limits")
        with self._lock:
            self._ensure_ready()
            if self._armed:
                self._target = degrees
                self._tilt_target = tilt_degrees
                self._target_updated_at = produced_at

    def disable_tilt(self) -> None:
        """Drop the configured tilt channel without returning it to neutral."""
        with self._lock:
            self._tilt_target = None

    def pause_motion(self) -> None:
        """Clear pending output while retaining the tracking send gate."""
        with self._lock:
            self._target = self._tilt_target = None
            self._target_updated_at = None

    def stop_motion(self) -> None:
        with self._lock:
            self._armed = False
            self._target = None
            self._tilt_target = None
            self._target_updated_at = None

    def snapshot(self) -> ServoHealth:
        with self._lock:
            return ServoHealth(
                self._ready,
                self._armed,
                self._target,
                self._last_error,
            )

    def close(self, *, timeout: float = 1.0) -> bool:
        with self._lock:
            self._armed = False
            self._target = None
            self._tilt_target = None
            self._target_updated_at = None
            self._ready = False
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self.writer.close()
            with self._lock:
                self._thread = None
        return stopped

    def _run(self) -> None:
        while not self._stop.wait(self.period):
            self._send_target()

    def _send_target(self) -> None:
        with self._lock:
            if not self._ready or not self._armed or self._target is None:
                return
            age = (self.clock() - self._target_updated_at
                   if self._target_updated_at is not None else float("inf"))
            reason = "target_expired"
            if 0 <= age < TARGET_MAX_AGE_SECONDS:
                try:
                    payload = (encode_position(self._target, channel=self.pan_channel) if self.protocol == "m0_pulse"
                               else encode_command("SET", self._target))
                    if self._tilt_target is not None:
                        payload += encode_position(self._tilt_target, channel=self.tilt_channel)
                    self.writer.write(payload)
                    return
                except Exception:
                    reason = "transport_write_failed"
            self._last_error = reason
            self._ready = False
            self._armed = False
            self._target = None
            self._tilt_target = None
            self._target_updated_at = None
            self._stop.set()
        # Status callbacks can acquire the app control lock and call stop_motion.
        # Never invoke them while holding the serial writer lock.
        if self.on_error is not None:
            self.on_error(reason)

    def _ensure_ready(self) -> None:
        if not self._ready or self._last_error is not None:
            raise RuntimeError("Servo transport is not ready")


def serial_client(
    *,
    device: str,
    baud: int,
    angle_min_deg: float,
    angle_max_deg: float,
    send_hz: int,
    protocol: str = "shore_spotter",
    serial_factory: Callable[..., object] | None = None,
    tilt_min_deg: float = 85.0,
    tilt_max_deg: float = 95.0,
    pan_channel: int = 0,
    tilt_channel: int = 1,
) -> SimpleServoClient:
    return SimpleServoClient(
        _SerialWriter(
            device=device,
            baud=baud,
            protocol=protocol,
            serial_factory=serial_factory,
        ),
        angle_min_deg=angle_min_deg,
        angle_max_deg=angle_max_deg,
        send_hz=send_hz,
        protocol=protocol,
        tilt_min_deg=tilt_min_deg,
        tilt_max_deg=tilt_max_deg,
        pan_channel=pan_channel,
        tilt_channel=tilt_channel,
    )
