from __future__ import annotations

from shore_tracker.inference import (
    ActionPrediction,
    DetectionPrediction,
    NormalizedBox,
    TargetSelector,
    TargetSelectorConfig,
    TrackObservation,
)


def observation(track_id: int, *, chasing: float, x: float = 0.4) -> TrackObservation:
    return TrackObservation(
        detection=DetectionPrediction(
            track_id=track_id,
            box=NormalizedBox(x=x, y=0.2, width=0.1, height=0.2),
            confidence=0.9,
        ),
        action=ActionPrediction(
            track_id=track_id,
            chasing_wave=chasing,
            takeoff=0.0,
            surfing=0.0,
        ),
    )


def test_selector_waits_for_stability_and_picks_the_earliest_candidate() -> None:
    selector = TargetSelector(
        TargetSelectorConfig(
            stable_observations=3,
            stable_seconds=0.0,
            chasing_threshold=0.7,
        )
    )

    assert selector.observe([observation(20, chasing=0.9)], now=0.0) is None
    assert selector.observe(
        [observation(10, chasing=0.9), observation(20, chasing=0.9)], now=0.1
    ) is None

    # Track 20 started chasing first and wins even though track 10 also reaches a high score.
    assert selector.observe(
        [observation(10, chasing=0.99), observation(20, chasing=0.71)], now=0.2
    ) == 20


def test_selector_does_not_accumulate_non_consecutive_low_confidence_frames() -> None:
    selector = TargetSelector(
        TargetSelectorConfig(
            stable_observations=2,
            stable_seconds=0.0,
            chasing_threshold=0.7,
        )
    )

    assert selector.observe([observation(3, chasing=0.9)], now=0.0) is None
    assert selector.observe([observation(3, chasing=0.4)], now=0.1) is None
    assert selector.observe([observation(3, chasing=0.9)], now=0.2) is None
    assert selector.observe([observation(3, chasing=0.9)], now=0.3) == 3


def test_selector_expires_a_candidate_after_a_tracking_gap() -> None:
    selector = TargetSelector(
        TargetSelectorConfig(
            stable_observations=2,
            stable_seconds=0.0,
            chasing_threshold=0.7,
            candidate_gap_timeout_seconds=0.25,
        )
    )

    assert selector.observe([observation(8, chasing=0.9)], now=0.0) is None
    assert selector.observe([], now=0.3) is None
    assert selector.observe([observation(8, chasing=0.9)], now=0.4) is None
    assert selector.observe([observation(8, chasing=0.9)], now=0.5) == 8


def test_selector_requires_the_configured_candidate_hold_time() -> None:
    selector = TargetSelector(
        TargetSelectorConfig(
            stable_observations=2,
            stable_seconds=0.5,
            chasing_threshold=0.7,
        )
    )

    assert selector.observe([observation(5, chasing=0.9)], now=1.0) is None
    assert selector.observe([observation(5, chasing=0.9)], now=1.2) is None
    assert selector.observe([observation(5, chasing=0.9)], now=1.5) == 5
