import pytest

from shore_tracker.control import PanPlanner, PanPlannerConfig
from shore_tracker.inference import (
    ActionPrediction,
    DetectionPrediction,
    FollowDecision,
    FollowDecisionReason,
    FollowState,
    NormalizedBox,
    TrackObservation,
)


def _planner(direction: int = 1) -> PanPlanner:
    return PanPlanner(
        PanPlannerConfig(
            angle_min_deg=0,
            angle_max_deg=180,
            search_min_deg=20,
            search_max_deg=160,
            max_slew_deg_s=30,
            search_speed_deg_s=8,
            pan_gain_deg=45,
            deadband=0.04,
            direction=direction,
        )
    )


def _decision(center_x: float | None, *, following: bool = True) -> FollowDecision:
    if center_x is None:
        return FollowDecision(
            FollowState.CHASING if following else FollowState.SEARCHING,
            7 if following else None,
            None,
            FollowDecisionReason.TEMPORARILY_LOST if following else FollowDecisionReason.SEARCHING,
        )
    observation = TrackObservation(
        DetectionPrediction(7, NormalizedBox(center_x - 0.05, 0.3, 0.1, 0.2), 0.9),
        ActionPrediction(7, 0.9, 0.1, 0.1),
    )
    return FollowDecision(FollowState.CHASING, 7, observation, FollowDecisionReason.TRACKING)


def test_disarmed_never_changes_angle() -> None:
    planner = _planner()
    assert planner.step(now=0, current_angle_deg=90, decision=_decision(0.8), armed=False) == 90
    assert planner.step(now=1, current_angle_deg=90, decision=_decision(0.8), armed=False) == 90


def test_target_correction_obeys_slew_and_direction() -> None:
    planner = _planner(direction=1)
    planner.step(now=0, current_angle_deg=90, decision=_decision(0.8), armed=True)
    result = planner.step(now=0.1, current_angle_deg=90, decision=_decision(0.8), armed=True)
    assert result == pytest.approx(90.6)
    inverted = _planner(direction=-1)
    inverted.step(now=0, current_angle_deg=90, decision=_decision(0.8), armed=True)
    assert inverted.step(now=0.1, current_angle_deg=90, decision=_decision(0.8), armed=True) == pytest.approx(89.4)


def test_deadband_and_temporary_loss_hold() -> None:
    planner = _planner()
    planner.step(now=0, current_angle_deg=90, decision=_decision(0.51), armed=True)
    assert planner.step(now=0.1, current_angle_deg=90, decision=_decision(0.51), armed=True) == 90
    assert planner.step(now=0.2, current_angle_deg=90, decision=_decision(None), armed=True) == 90


def test_no_target_returns_to_ninety_and_holds() -> None:
    planner = _planner()
    searching = _decision(None, following=False)
    planner.step(now=0, current_angle_deg=94, decision=searching, armed=True)
    angle = 94.0
    for i in range(1, 25):
        next_angle = planner.step(now=i*.05, current_angle_deg=angle, decision=searching, armed=True)
        assert 90 <= next_angle <= angle
        angle = next_angle
    assert angle == 90



@pytest.mark.parametrize('angle', [70, 110])
def test_temporary_loss_holds_off_centre_angle(angle) -> None:
    planner = _planner()
    lost = _decision(None)
    planner.step(now=0, current_angle_deg=angle, decision=lost, armed=True)
    assert planner.step(now=0.1, current_angle_deg=angle, decision=lost, armed=True) == angle


def test_alternating_detection_does_not_reverse_toward_home():
    planner = _planner(direction=-1)
    angle = 90
    angles = []
    for i, center in enumerate([.8, .8, None, .8, None, .8]):
        angle = planner.step(now=i*.2, current_angle_deg=angle,
                             decision=_decision(center), armed=True)
        angles.append(angle)
    assert angles == pytest.approx([90, 89.4, 89.4, 88.8, 88.8, 88.2])


def test_uncalibrated_direction_is_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        _planner(direction=0)
