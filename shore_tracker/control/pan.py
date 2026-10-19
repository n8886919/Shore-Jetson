from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt

from shore_tracker.inference import FollowDecision, FollowDecisionReason


@dataclass(frozen=True, slots=True)
class PanPlannerConfig:
    angle_min_deg: float
    angle_max_deg: float
    search_min_deg: float
    search_max_deg: float
    max_slew_deg_s: float
    search_speed_deg_s: float
    pan_gain_deg: float
    deadband: float
    direction: int
    home_deg: float = 90.0
    max_step_seconds: float = 0.10
    max_acceleration_deg_s2: float = 60.0
    gain_reference_seconds: float = 0.20

    def __post_init__(self) -> None:
        values = (
            self.angle_min_deg,
            self.angle_max_deg,
            self.search_min_deg,
            self.search_max_deg,
            self.max_slew_deg_s,
            self.search_speed_deg_s,
            self.pan_gain_deg,
            self.deadband,
            self.max_step_seconds,
            self.home_deg,
            self.max_acceleration_deg_s2,
            self.gain_reference_seconds,
        )
        if not all(isfinite(value) for value in values):
            raise ValueError("pan planner values must be finite")
        if not (
            self.angle_min_deg
            <= self.search_min_deg
            < self.search_max_deg
            <= self.angle_max_deg
        ):
            raise ValueError("search range must be inside profile range")
        if self.max_slew_deg_s <= 0 or not 0 < self.search_speed_deg_s <= self.max_slew_deg_s:
            raise ValueError("pan speeds are invalid")
        if self.max_acceleration_deg_s2 <= 0 or self.gain_reference_seconds <= 0:
            raise ValueError("pan acceleration and gain reference must be positive")
        if self.pan_gain_deg <= 0 or not 0 <= self.deadband < 0.5:
            raise ValueError("pan gain/deadband are invalid")
        if self.direction not in {-1, 1}:
            raise ValueError("direction must be calibrated to -1 or +1")
        if not self.search_min_deg <= self.home_deg <= self.search_max_deg:
            raise ValueError("home angle must be inside the tracking range")
        if not 0 < self.max_step_seconds <= 0.5:
            raise ValueError("max_step_seconds is invalid")


class PanPlanner:
    """Turn target observations into bounded positional-Servo commands.

    This planner limits requested angles, speed and acceleration. With direct
    M0 UART the controller generates PWM, without a known command watchdog
    or position feedback; current_angle_deg is the last commanded position.
    """

    def __init__(self, config: PanPlannerConfig) -> None:
        self.config = config
        self._last_at: float | None = None
        self._velocity = 0.0

    def reset(self) -> None:
        self._last_at = None
        self._velocity = 0.0

    def step(
        self,
        *,
        now: float,
        current_angle_deg: float,
        decision: FollowDecision,
        armed: bool,
    ) -> float:
        if not isfinite(now) or not isfinite(current_angle_deg):
            raise ValueError("time and current angle must be finite")
        if not self.config.angle_min_deg <= current_angle_deg <= self.config.angle_max_deg:
            raise ValueError("current angle is outside the profile")
        if self._last_at is not None and now < self._last_at:
            raise ValueError("now must be monotonic")
        if not armed:
            self._velocity = 0.0
            self._last_at = now
            return current_angle_deg
        if self._last_at is None:
            self._last_at = now
            return current_angle_deg
        elapsed = min(now - self._last_at, self.config.max_step_seconds)
        self._last_at = now
        if elapsed <= 0:
            return current_angle_deg

        if (decision.target is None and decision.target_track_id is not None
                and decision.reason is FollowDecisionReason.TEMPORARILY_LOST):
            self._velocity = 0.0
            return current_angle_deg

        distance = None
        if decision.target is not None:
            error = decision.target.detection.box.pan_error
            # Keep the familiar gain scale at a fixed 200 ms reference, so
            # actual inference frequency never multiplies controller strength.
            requested = (0.0 if abs(error) <= self.config.deadband else
                         self.config.direction * error * self.config.pan_gain_deg
                         / self.config.gain_reference_seconds)
        else:
            distance = self.config.home_deg - current_angle_deg
            if abs(distance) < 1e-9:
                self._velocity = 0.0
                return self.config.home_deg
            braking_speed = sqrt(2 * self.config.max_acceleration_deg_s2 * abs(distance))
            requested = braking_speed if distance > 0 else -braking_speed
        requested = self._clamp(requested, -self.config.max_slew_deg_s, self.config.max_slew_deg_s)
        max_change = self.config.max_acceleration_deg_s2 * elapsed
        self._velocity += self._clamp(requested - self._velocity, -max_change, max_change)
        delta = self._velocity * elapsed
        if distance is not None and delta * distance > 0 and abs(delta) >= abs(distance):
            self._velocity = 0.0
            return self.config.home_deg
        desired = current_angle_deg + delta
        bounded = self._clamp(desired, self.config.search_min_deg, self.config.search_max_deg)
        if desired != bounded: self._velocity = 0.0
        return bounded

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    @staticmethod
    def _slew(current: float, desired: float, elapsed: float, rate: float) -> float:
        maximum_delta = elapsed * rate
        delta = desired - current
        if abs(delta) <= maximum_delta:
            return desired
        return current + (maximum_delta if delta > 0 else -maximum_delta)
