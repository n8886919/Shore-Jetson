"""Backend-neutral inference values used after object tracking.

The trained detector and action classifier are deliberately kept separate from
these values. The native TensorRT adapter feeds the same target-selection policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


ACTION_NAMES = ("chasing_wave", "takeoff", "surfing")


def _finite_unit_interval(name: str, value: float) -> None:
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a finite value in [0, 1]")


def _track_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("track_id must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class NormalizedBox:
    """An ``xywh`` box normalized to the full video frame."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        for name, value in (
            ("x", self.x),
            ("y", self.y),
            ("width", self.width),
            ("height", self.height),
        ):
            if not isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.x < 0.0 or self.y < 0.0:
            raise ValueError("x and y must be non-negative")
        if self.width <= 0.0 or self.height <= 0.0:
            raise ValueError("width and height must be positive")
        if self.x + self.width > 1.0 or self.y + self.height > 1.0:
            raise ValueError("box must fit inside the normalized frame")

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2.0

    @property
    def pan_error(self) -> float:
        """Signed horizontal error from frame center, in normalized units."""

        return self.center_x - 0.5

    @property
    def tilt_error(self) -> float:
        """Positive below frame center; pulse direction must be calibrated."""
        return self.y + self.height / 2.0 - 0.5


@dataclass(frozen=True, slots=True)
class DetectionPrediction:
    """A surfer detection after the tracker has assigned a stable ID."""

    track_id: int
    box: NormalizedBox
    confidence: float

    def __post_init__(self) -> None:
        _track_id(self.track_id)
        _finite_unit_interval("confidence", self.confidence)


@dataclass(frozen=True, slots=True)
class ActionPrediction:
    """Independent sigmoid probabilities in SurfTrack checkpoint order."""

    track_id: int
    chasing_wave: float
    takeoff: float
    surfing: float

    def __post_init__(self) -> None:
        _track_id(self.track_id)
        for name, value in self.probabilities.items():
            _finite_unit_interval(name, value)

    @property
    def probabilities(self) -> dict[str, float]:
        return {
            "chasing_wave": self.chasing_wave,
            "takeoff": self.takeoff,
            "surfing": self.surfing,
        }

    def is_chasing(self, threshold: float) -> bool:
        _finite_unit_interval("threshold", threshold)
        return self.chasing_wave >= threshold

    def is_active(self, threshold: float) -> bool:
        """Whether the surfer is anywhere in the chase/takeoff/ride sequence."""

        _finite_unit_interval("threshold", threshold)
        return max(self.chasing_wave, self.takeoff, self.surfing) >= threshold


@dataclass(frozen=True, slots=True)
class TrackObservation:
    detection: DetectionPrediction
    action: ActionPrediction

    def __post_init__(self) -> None:
        if self.detection.track_id != self.action.track_id:
            raise ValueError("detection and action track_id must match")

    @property
    def track_id(self) -> int:
        return self.detection.track_id
