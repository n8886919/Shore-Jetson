from __future__ import annotations

import threading
import time
from math import isfinite
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ComponentState:
    state: str = "waiting"
    detail: str = ""
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class TrackingState:
    armed: bool = False
    mode: str = "disarmed"
    target_track_id: int | None = None
    target_x: float | None = None
    chasing_wave: float | None = None
    takeoff: float | None = None
    surfing: float | None = None
    reason: str = "boot requires manual ARM"


class StatusStore:
    """Thread-safe, secret-free state exposed to the local dashboard."""

    COMPONENTS = ("camera", "inference", "servo", "recording")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._components = {name: ComponentState() for name in self.COMPONENTS}
        self._tracking = TrackingState()
        self._observations: tuple = ()
        self._observations_received = False
        self._observations_at: float | None = None
        self._observation_sequence = 0
        self._recognition_thresholds = {'detection': .5, 'chasing_wave': .7, 'takeoff': .65, 'surfing': .7}
        self._recognition_stale_seconds = 1.5
        self._free_gib: float | None = None
        self._camera_frame_age_ms: int | None = None
        self._inference_frame_age_ms: int | None = None
        self._servo_angle_deg: float | None = None
        self._tilt_angle_deg: float | None = None
        self._servo_command = {"protocol": None, "pan_speed": None, "tilt_speed": None,
                               "position_feedback": False}
        self._tilt_enabled = False
        self._tilt_available = False
        self._disk_pressure = "unknown"
        self._zoom_scale = 1.0
        self._zoom_center_x = 0.5
        self._target_speed = 0.0
        self._inference_fps: float | None = None
        self._last_recording: str | None = None
        self._pan_gain_deg = 45.0
        self._pan_deadband = 0.20
        self._pan_direction = 1
        self._zoom_maximum = 2.5
        self._disarm_listeners: list[Callable[[str], None]] = []
        self._run_id = 0
        self._run_started = 0.0

    def add_disarm_listener(self, callback: Callable[[str], None]) -> None:
        """Register a fail-safe callback without invoking it for boot DISARM."""

        if not callable(callback):
            raise TypeError("disarm listener must be callable")
        with self._lock:
            self._disarm_listeners.append(callback)

    def update_component(self, name: str, state: str, detail: str = "") -> None:
        if name not in self._components:
            raise KeyError(name)
        notify = False
        reason = f"{name}: {state}"
        with self._lock:
            self._components[name] = ComponentState(
                state=state,
                detail=detail,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            if (
                name in {"camera", "inference", "servo"}
                and state != "ready" and not (name == "inference" and state == "idle")
            ):
                notify = self._disarm_locked(reason)
        if notify:
            self._notify_disarm(reason)

    def update_metrics(
        self,
        *,
        free_gib: float | None = None,
        camera_frame_age_ms: int | None = None,
        inference_frame_age_ms: int | None = None,
        servo_angle_deg: float | None = None,
        tilt_angle_deg: float | None = None,
        disk_pressure: str | None = None,
        zoom_scale: float | None = None,
        zoom_center_x: float | None = None,
        target_speed: float | None = None,
        inference_fps: float | None = None,
        last_recording: str | None = None,
    ) -> None:
        with self._lock:
            if free_gib is not None:
                self._free_gib = free_gib
            if camera_frame_age_ms is not None:
                self._camera_frame_age_ms = camera_frame_age_ms
            if inference_frame_age_ms is not None:
                self._inference_frame_age_ms = inference_frame_age_ms
            if servo_angle_deg is not None:
                self._servo_angle_deg = servo_angle_deg
            if tilt_angle_deg is not None:
                self._tilt_angle_deg = tilt_angle_deg
            if disk_pressure is not None:
                self._disk_pressure = disk_pressure
            if zoom_scale is not None:
                self._zoom_scale = max(1.0, float(zoom_scale))
            if zoom_center_x is not None:
                self._zoom_center_x = min(1.0, max(0.0, float(zoom_center_x)))
            if target_speed is not None:
                self._target_speed = max(0.0, float(target_speed))
            if inference_fps is not None:
                self._inference_fps = max(0.0, float(inference_fps))
            if last_recording is not None:
                self._last_recording = last_recording

    def update_observations(self, observations, *, inference_run_id=None) -> bool:
        """Publish immutable model outputs independently of ARM/target selection."""
        with self._lock:
            if inference_run_id is not None and (not self._tracking.armed or inference_run_id != self._run_id):
                return False
            self._observations = tuple(observations)
            self._observations_received = True
            self._observations_at = time.monotonic()
            self._observation_sequence += 1
            return True

    def update_servo_command(self, *, protocol: str, pan: int | None, tilt: int | None,
                             run_id: int | None = None) -> None:
        """Requested protocol levels; these are never measured motion or angles."""
        with self._lock:
            if run_id is not None and (not self._tracking.armed or self._run_id != run_id):
                return
            self._servo_command = {"protocol": protocol, "pan_speed": pan, "tilt_speed": tilt,
                                   "position_feedback": False}
            if protocol == "pelco_d":
                self._servo_angle_deg = self._tilt_angle_deg = None

    def configure_recognition(self, *, detection=None, chasing_wave=None, takeoff=None, surfing=None, stale_seconds=None):
        values = {k: v for k, v in dict(detection=detection, chasing_wave=chasing_wave, takeoff=takeoff, surfing=surfing).items() if v is not None}
        if any(not isfinite(v) or not 0 <= v <= 1 for v in values.values()):
            raise ValueError('recognition thresholds must be finite in [0, 1]')
        if stale_seconds is not None and (not isfinite(stale_seconds) or stale_seconds <= 0):
            raise ValueError('recognition freshness must be positive')
        with self._lock:
            self._recognition_thresholds.update(values)
            if stale_seconds is not None: self._recognition_stale_seconds = stale_seconds

    def _recognition_snapshot(self) -> dict[str, Any]:
        age = time.monotonic() - self._observations_at if self._observations_at is not None else float('inf')
        available = self._observations_received and age < self._recognition_stale_seconds and all(
            self._components[name].state == "ready" for name in ("camera", "inference")
        )
        items = tuple(item for item in self._observations if item.detection.confidence >= self._recognition_thresholds['detection']) if available else ()
        selected = next((item for item in items
                         if self._tracking.armed and item.track_id == self._tracking.target_track_id), None)
        source = "locked" if selected else "candidate"
        if selected is None:
            selected = max(items, key=lambda item: (item.detection.confidence, -item.track_id), default=None)
        return {
            "available": available,
            "sequence": self._observation_sequence if available else None,
            "valid_for_ms": max(0, round((self._recognition_stale_seconds - age) * 1000)) if available else 0,
            "detections": [
                {"track_id": item.track_id, "box": asdict(item.detection.box),
                 "confidence": item.detection.confidence,
                 "states": [key for key, probability in item.action.probabilities.items()
                            if probability >= self._recognition_thresholds[key]]}
                for item in items
            ],
            "detection_count": len(items) if available else None,
            "source": source if selected else None,
            "track_id": selected.track_id if selected else None,
            "confidence": selected.detection.confidence if selected else None,
            "chasing_wave": selected.action.chasing_wave if selected else None,
            "takeoff": selected.action.takeoff if selected else None,
            "surfing": selected.action.surfing if selected else None,
        }

    def update_target(
        self,
        *,
        mode: str,
        track_id: int | None,
        target_x: float | None = None,
        chasing_wave: float | None = None,
        takeoff: float | None = None,
        surfing: float | None = None,
        reason: str = "",
    ) -> None:
        with self._lock:
            self._tracking.mode = mode
            self._tracking.target_track_id = track_id
            self._tracking.target_x = target_x
            self._tracking.chasing_wave = chasing_wave
            self._tracking.takeoff = takeoff
            self._tracking.surfing = surfing
            if reason:
                self._tracking.reason = reason

    def update_tuning(
        self,
        *,
        pan_gain_deg: float,
        pan_deadband: float,
        pan_direction: int,
        zoom_maximum: float,
        tilt_enabled: bool | None = None,
        tilt_available: bool | None = None,
    ) -> None:
        with self._lock:
            self._pan_gain_deg = pan_gain_deg
            self._pan_deadband = pan_deadband
            self._pan_direction = pan_direction
            self._zoom_maximum = zoom_maximum
            if tilt_enabled is not None:
                self._tilt_enabled = tilt_enabled
            if tilt_available is not None:
                self._tilt_available = tilt_available

    def can_arm(self) -> tuple[bool, str]:
        with self._lock:
            if self._tracking.armed:
                return False, "already armed"
            if self._tracking.mode == "arming":
                return False, "ARM request already in progress"
            required = ("camera", "inference", "servo")
            blocked = [
                f"{name}={self._components[name].state}"
                for name in required
                if self._components[name].state not in ({"ready", "idle"} if name == "inference" else {"ready"})
            ]
            if blocked:
                return False, ", ".join(blocked)
            return True, "ready"

    def arm(self) -> tuple[bool, str]:
        """Immediately arm a hardware-free/simulated runtime."""

        allowed, reason = self.request_arm()
        if not allowed:
            return False, reason
        self.confirm_armed("manual ARM")
        return True, "armed"

    def request_arm(self) -> tuple[bool, str]:
        with self._lock:
            allowed, reason = self.can_arm()
            if not allowed:
                return False, reason
            self._tracking.armed = False
            self._tracking.mode = "arming"
            self._tracking.reason = "sending Servo ARM"
            return True, "arming"

    def confirm_armed(self, reason: str = "manual ARM") -> None:
        with self._lock:
            if self._tracking.mode != "arming":
                return
            self._tracking.armed = True
            self._run_id += 1
            self._run_started = time.monotonic()
            self._tracking.mode = "searching"
            self._tracking.reason = reason

    def arm_request_active(self) -> bool:
        with self._lock:
            return self._tracking.mode == "arming" and not self._tracking.armed

    def inference_run(self):
        with self._lock:
            return (self._run_id, self._run_started) if self._tracking.armed else None

    def disarm(self, reason: str) -> None:
        notify = False
        with self._lock:
            notify = self._disarm_locked(reason)
        if notify:
            self._notify_disarm(reason)

    def _disarm_locked(self, reason: str) -> bool:
        changed = self._tracking.armed or self._tracking.mode == "arming"
        self._tracking.armed = False
        self._tracking.mode = "disarmed"
        self._tracking.target_track_id = None
        self._tracking.reason = reason
        self._observations = ()
        self._observations_received = False
        self._observations_at = None
        self._inference_fps = 0.0
        return changed

    def _notify_disarm(self, reason: str) -> None:
        with self._lock:
            listeners = tuple(self._disarm_listeners)
        for callback in listeners:
            try:
                callback(reason)
            except Exception:
                # Local DISARM has already won. A transport callback must never
                # unwind the status path; its independent watchdog is the final
                # hardware boundary.
                continue

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            allowed, arm_reason = self.can_arm()
            return {
                "time": datetime.now(timezone.utc).isoformat(),
                "components": {
                    name: asdict(value) for name, value in self._components.items()
                },
                "tracking": asdict(self._tracking),
                "servo_command": dict(self._servo_command),
                "inference_run_id": self._run_id if self._tracking.armed else None,
                "recognition": self._recognition_snapshot(),
                "arm_allowed": allowed,
                "arm_reason": arm_reason,
                "metrics": {
                    "free_gib": self._free_gib,
                    "camera_frame_age_ms": self._camera_frame_age_ms,
                    "inference_frame_age_ms": self._inference_frame_age_ms,
                    "servo_angle_deg": self._servo_angle_deg,
                    "tilt_angle_deg": self._tilt_angle_deg,
                    "disk_pressure": self._disk_pressure,
                    "zoom_scale": self._zoom_scale,
                    "zoom_center_x": self._zoom_center_x,
                    "target_speed": self._target_speed,
                    "inference_fps": self._inference_fps,
                    "last_recording": self._last_recording,
                },
                "tuning": {
                    "pan_gain_deg": self._pan_gain_deg,
                    "pan_deadband": self._pan_deadband,
                    "pan_direction": self._pan_direction,
                    "zoom_maximum": self._zoom_maximum,
                    "tilt_enabled": self._tilt_enabled,
                    "tilt_available": self._tilt_available,
                },
            }
