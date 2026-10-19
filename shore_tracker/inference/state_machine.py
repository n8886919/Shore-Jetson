"""Temporal target-lock policy independent of any inference runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from math import isfinite
from typing import Iterable

from .selection import TargetSelector, TargetSelectorConfig
from .types import TrackObservation


class FollowState(str, Enum):
    SEARCHING = "searching"
    CHASING = "chasing"
    TAKEOFF = "takeoff"
    SURFING = "surfing"
    FINISHED = "finished"


class FollowDecisionReason(str, Enum):
    SEARCHING = "searching"
    ACQUIRED = "acquired"
    TRACKING = "tracking"
    INACTIVE_GRACE = "inactive_grace"
    TEMPORARILY_LOST = "temporarily_lost"
    FINISH_TIMEOUT = "finish_timeout"
    LOST_TIMEOUT = "lost_timeout"
    PHASE_CHANGED = "phase_changed"
    CHASE_RECORDING = "chase_recording"
    NO_TAKEOFF = "no_takeoff"


@dataclass(frozen=True, slots=True)
class TargetFollowConfig:
    selector: TargetSelectorConfig = field(default_factory=TargetSelectorConfig)
    active_chasing_threshold: float = 0.70
    takeoff_threshold: float = 0.65
    surfing_threshold: float = 0.70
    finish_timeout_seconds: float = 2.00
    ride_exit_seconds: float = 5.00

    def __post_init__(self) -> None:
        for name, value in (
            ("active_chasing_threshold", self.active_chasing_threshold),
            ("takeoff_threshold", self.takeoff_threshold),
            ("surfing_threshold", self.surfing_threshold),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        for name, value in (
            ("finish_timeout_seconds", self.finish_timeout_seconds),
            ("ride_exit_seconds", self.ride_exit_seconds),
        ):
            if not isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")

    @classmethod
    def from_values(
        cls,
        *,
        chasing_threshold: float,
        takeoff_threshold: float,
        surfing_threshold: float,
        candidate_hold_seconds: float,
        finished_hold_seconds: float,
        ride_exit_seconds: float = 5.00,
        stable_observations: int = 3,
        candidate_gap_timeout_seconds: float = 0.50,
    ) -> "TargetFollowConfig":
        """Map runtime settings without importing the runtime config module."""

        return cls(
            selector=TargetSelectorConfig(
                chasing_threshold=chasing_threshold,
                takeoff_threshold=takeoff_threshold,
                surfing_threshold=surfing_threshold,
                stable_observations=stable_observations,
                stable_seconds=candidate_hold_seconds,
                candidate_gap_timeout_seconds=candidate_gap_timeout_seconds,
            ),
            active_chasing_threshold=chasing_threshold,
            takeoff_threshold=takeoff_threshold,
            surfing_threshold=surfing_threshold,
            finish_timeout_seconds=finished_hold_seconds,
            ride_exit_seconds=ride_exit_seconds,
        )


@dataclass(frozen=True, slots=True)
class FollowDecision:
    state: FollowState
    target_track_id: int | None
    target: TrackObservation | None
    reason: FollowDecisionReason
    recording_valid: bool = False


class TargetFollowStateMachine:
    """Record chasing first; lock the first qualifying takeoff/ride from that pool."""

    def __init__(self, config: TargetFollowConfig | None = None) -> None:
        self.config = config or TargetFollowConfig()
        self._chase_selector = TargetSelector(replace(self.config.selector, actions=("chasing_wave",)))
        self._selector = TargetSelector(replace(self.config.selector, actions=("takeoff", "surfing")))
        self._last_now = None
        self._release()

    @property
    def state(self): return self._phase

    @property
    def target_track_id(self): return self._target_track_id

    def reset(self):
        self._release()
        self._last_now = None

    def _result(self, reason, target=None):
        return FollowDecision(self._phase, self._target_track_id, target, reason, self._qualified)

    def update(self, observations: Iterable[TrackObservation], *, now: float) -> FollowDecision:
        self._validate_time(now)
        indexed = {}
        for item in observations:
            if item.track_id in indexed: raise ValueError(f"duplicate track_id in frame: {item.track_id}")
            indexed[item.track_id] = item
        if self._phase is FollowState.FINISHED:
            if now - self._finished_at >= self.config.finish_timeout_seconds:
                self._release()
            return self._result(FollowDecisionReason.SEARCHING if self._phase is FollowState.SEARCHING else FollowDecisionReason.FINISH_TIMEOUT)

        if self._phase is FollowState.SEARCHING:
            candidate = self._chase_selector.observe(indexed.values(), now=now)
            if candidate is None: return self._result(FollowDecisionReason.SEARCHING)
            self._phase = FollowState.CHASING
            self._last_trial_active_at = now
            self._chasers_seen = {item.track_id for item in indexed.values()
                                  if item.action.chasing_wave >= self.config.active_chasing_threshold}
            # Starting to record is distinct from assigning a pan target.
            return self._result(FollowDecisionReason.CHASE_RECORDING)

        if self._target_track_id is None:
            eligible = [item for item in indexed.values() if item.track_id in self._chasers_seen]
            chosen = self._selector.observe(eligible, now=now)
            if chosen is not None:
                self._target_track_id = chosen
                self._qualified = True
                self._last_active_at = now
                target = indexed[chosen]
                self._phase = (FollowState.SURFING if target.action.surfing >= self.config.surfing_threshold
                               else FollowState.TAKEOFF)
                return self._result(FollowDecisionReason.ACQUIRED, target)
            chasing = {item.track_id for item in indexed.values()
                       if item.action.chasing_wave >= self.config.active_chasing_threshold}
            self._chasers_seen.update(chasing)
            if chasing or any(self._is_active(item) for item in eligible):
                self._last_trial_active_at = now
            elif now - self._last_trial_active_at >= self.config.finish_timeout_seconds:
                self._phase = FollowState.FINISHED
                self._finished_at = now
                return self._result(FollowDecisionReason.NO_TAKEOFF)
            return self._result(FollowDecisionReason.CHASE_RECORDING)

        target = indexed.get(self._target_track_id)
        # The deadline starts at the last valid ride observation, not at the
        # next missing/low-score frame. Late recovery cannot extend a finished ride.
        if now - self._last_active_at >= self.config.ride_exit_seconds:
            self._phase = FollowState.FINISHED
            self._finished_at = now
            return self._result(FollowDecisionReason.LOST_TIMEOUT if target is None else FollowDecisionReason.FINISH_TIMEOUT)
        if target is not None and self._is_active(target):
            self._last_active_at = now
            previous = self._phase
            if target.action.surfing >= self.config.surfing_threshold:
                self._phase = FollowState.SURFING
            return self._result(FollowDecisionReason.PHASE_CHANGED if previous != self._phase else FollowDecisionReason.TRACKING, target)
        # Missing and low-score observations share the same deadline. Seeing
        # the box alone does not reset the last valid ride observation.
        if target is None:
            return self._result(FollowDecisionReason.TEMPORARILY_LOST)
        return self._result(FollowDecisionReason.INACTIVE_GRACE, target)

    def process(self, now: float, observations: Iterable[TrackObservation]) -> FollowDecision:
        return self.update(observations, now=now)

    def _is_active(self, target):
        # Returning to chasing/paddling is the end of a qualified ride.
        return (target.action.takeoff >= self.config.takeoff_threshold
                or target.action.surfing >= self.config.surfing_threshold)

    def _release(self):
        self._phase = FollowState.SEARCHING
        self._target_track_id = None
        self._qualified = False
        self._chasers_seen = set()
        self._last_active_at = None
        self._last_trial_active_at = None
        self._finished_at = None
        self._selector.reset()
        self._chase_selector.reset()

    def _validate_time(self, now):
        if not isfinite(now): raise ValueError("now must be finite")
        if self._last_now is not None and now < self._last_now: raise ValueError("now must be monotonic")
        self._last_now = now
