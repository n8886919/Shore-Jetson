from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from shore_tracker.inference import FollowDecision, FollowState


@dataclass(frozen=True, slots=True)
class ZoomPlannerConfig:
    maximum: float = 2.5
    target_box_height: float = 0.28
    edge_margin: float = 0.18
    fast_target_speed: float = 0.35
    slow_inference_seconds: float = 0.35
    zoom_in_per_second: float = 0.35
    zoom_out_per_second: float = 4.0


@dataclass(frozen=True, slots=True)
class ZoomDecision:
    scale: float
    center_x: float
    target_speed: float
    reason: str


class ZoomPlanner:
    """Stable-first digital framing: fast out, deliberately slow in.

    Detection always runs on the original wide frame. This value only controls
    the kiosk preview crop, so zoom can never hide a surfer from inference.
    """

    def __init__(self, config: ZoomPlannerConfig) -> None:
        self.config = config
        self.scale = 1.0
        self._last_at: float | None = None
        self._last_track_id: int | None = None
        self._last_center_x: float | None = None

    def reset(self) -> ZoomDecision:
        self.scale = 1.0
        self._last_at = None
        self._last_track_id = None
        self._last_center_x = None
        return ZoomDecision(1.0, 0.5, 0.0, "wide_search")

    def step(
        self,
        decision: FollowDecision,
        *,
        now: float,
        inference_interval_seconds: float,
    ) -> ZoomDecision:
        if not isfinite(now) or not isfinite(inference_interval_seconds):
            raise ValueError("zoom timing must be finite")
        target = decision.target
        if target is None or decision.state in {FollowState.SEARCHING, FollowState.FINISHED}:
            return self.reset()

        center = target.detection.box.center_x
        elapsed = 0.0 if self._last_at is None else max(0.0, now - self._last_at)
        speed = 0.0
        if elapsed > 0 and self._last_track_id == target.track_id and self._last_center_x is not None:
            speed = abs(center - self._last_center_x) / elapsed
        self._last_at = now
        self._last_track_id = target.track_id
        self._last_center_x = center

        edge = center < self.config.edge_margin or center > 1 - self.config.edge_margin
        slow_inference = inference_interval_seconds >= self.config.slow_inference_seconds
        fast_target = speed >= self.config.fast_target_speed
        if edge or slow_inference or fast_target:
            self.scale = 1.0
            return ZoomDecision(1.0, center, speed, "wider_fov")
        else:
            desired = min(
                self.config.maximum,
                max(1.0, self.config.target_box_height / target.detection.box.height),
            )
            reason = "stable_subject_framing"

        rate = self.config.zoom_in_per_second if desired > self.scale else self.config.zoom_out_per_second
        maximum_change = rate * elapsed
        if elapsed == 0:
            next_scale = 1.0
        elif abs(desired - self.scale) <= maximum_change:
            next_scale = desired
        else:
            next_scale = self.scale + (maximum_change if desired > self.scale else -maximum_change)
        self.scale = min(self.config.maximum, max(1.0, next_scale))
        return ZoomDecision(self.scale, center, speed, reason)
