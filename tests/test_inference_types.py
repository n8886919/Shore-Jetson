from __future__ import annotations

import pytest

from shore_tracker.inference import (
    ActionPrediction,
    DetectionPrediction,
    NormalizedBox,
    TrackObservation,
)


def test_normalized_box_exposes_pan_error_around_frame_center() -> None:
    left = NormalizedBox(x=0.10, y=0.20, width=0.20, height=0.30)
    right = NormalizedBox(x=0.70, y=0.20, width=0.20, height=0.30)

    assert left.center_x == pytest.approx(0.20)
    assert left.pan_error == pytest.approx(-0.30)
    assert right.pan_error == pytest.approx(0.30)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"x": -0.01, "y": 0.0, "width": 0.1, "height": 0.1},
        {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.1},
        {"x": 0.9, "y": 0.0, "width": 0.2, "height": 0.1},
        {"x": 0.0, "y": 0.9, "width": 0.1, "height": 0.2},
    ],
)
def test_normalized_box_rejects_coordinates_outside_the_frame(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        NormalizedBox(**kwargs)


def test_action_prediction_uses_the_three_checkpoint_labels() -> None:
    action = ActionPrediction(
        track_id=7,
        chasing_wave=0.20,
        takeoff=0.75,
        surfing=0.30,
    )

    assert action.probabilities == {
        "chasing_wave": 0.20,
        "takeoff": 0.75,
        "surfing": 0.30,
    }
    assert action.is_active(0.70)
    assert not action.is_chasing(0.25)


def test_track_observation_rejects_mismatched_track_ids() -> None:
    detection = DetectionPrediction(
        track_id=3,
        box=NormalizedBox(0.1, 0.1, 0.2, 0.2),
        confidence=0.8,
    )
    action = ActionPrediction(4, chasing_wave=0.9, takeoff=0.0, surfing=0.0)

    with pytest.raises(ValueError, match="track_id"):
        TrackObservation(detection=detection, action=action)
