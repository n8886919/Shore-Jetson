"""Deterministic selection of the earliest stable active surfer."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable

from .types import TrackObservation


@dataclass(frozen=True, slots=True)
class TargetSelectorConfig:
    chasing_threshold: float = 0.70
    takeoff_threshold: float = 0.65
    surfing_threshold: float = 0.70
    stable_observations: int = 3
    stable_seconds: float = 0.50
    candidate_gap_timeout_seconds: float = 0.50
    actions: tuple[str, ...] = ("chasing_wave", "takeoff", "surfing")

    def __post_init__(self) -> None:
        if not self.actions or not set(self.actions) <= {"chasing_wave", "takeoff", "surfing"}:
            raise ValueError("invalid candidate actions")
        for name in ("chasing_threshold", "takeoff_threshold", "surfing_threshold"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if isinstance(self.stable_observations, bool) or self.stable_observations < 1:
            raise ValueError("stable_observations must be at least 1")
        for name, value in (
            ("stable_seconds", self.stable_seconds),
            ("candidate_gap_timeout_seconds", self.candidate_gap_timeout_seconds),
        ):
            if not isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(slots=True)
class _Candidate:
    first_active_at: float
    last_active_at: float
    observations: int


class TargetSelector:
    """Require stable evidence from any surf action, then choose whoever began first.

    A brief missing detection may preserve a candidate, but a visible prediction
    below the chasing threshold resets it immediately. Ties are resolved by
    tracker ID so the result is independent of input ordering.
    """

    def __init__(self, config: TargetSelectorConfig | None = None) -> None:
        self.config = config or TargetSelectorConfig()
        self._candidates: dict[int, _Candidate] = {}
        self._last_now: float | None = None

    def reset(self) -> None:
        self._candidates.clear()
        self._last_now = None

    def observe(self, observations: Iterable[TrackObservation], *, now: float) -> int | None:
        self._validate_time(now)
        indexed: dict[int, TrackObservation] = {}
        for observation in observations:
            if observation.track_id in indexed:
                raise ValueError(f"duplicate track_id in frame: {observation.track_id}")
            indexed[observation.track_id] = observation

        timeout = self.config.candidate_gap_timeout_seconds
        for track_id, candidate in tuple(self._candidates.items()):
            if now - candidate.last_active_at > timeout:
                self._candidates.pop(track_id)

        qualifying_now: set[int] = set()
        for track_id, observation in indexed.items():
            action = observation.action
            thresholds = {"chasing_wave": self.config.chasing_threshold,
                          "takeoff": self.config.takeoff_threshold,
                          "surfing": self.config.surfing_threshold}
            active = any(getattr(action, name) >= thresholds[name] for name in self.config.actions)
            if not active:
                self._candidates.pop(track_id, None)
                continue
            qualifying_now.add(track_id)
            candidate = self._candidates.get(track_id)
            if candidate is None:
                self._candidates[track_id] = _Candidate(now, now, 1)
            else:
                candidate.last_active_at = now
                candidate.observations += 1

        eligible = (
            (candidate.first_active_at, track_id)
            for track_id, candidate in self._candidates.items()
            if track_id in qualifying_now
            and candidate.observations >= self.config.stable_observations
            and now - candidate.first_active_at >= self.config.stable_seconds
        )
        selected = min(eligible, default=None)
        return selected[1] if selected is not None else None

    def _validate_time(self, now: float) -> None:
        if not isfinite(now):
            raise ValueError("now must be finite")
        if self._last_now is not None and now < self._last_now:
            raise ValueError("now must be monotonic")
        self._last_now = now
