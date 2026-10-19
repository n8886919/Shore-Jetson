from shore_tracker.control.zoom import ZoomPlanner, ZoomPlannerConfig
from shore_tracker.inference import ActionPrediction, DetectionPrediction, FollowDecision, FollowDecisionReason, FollowState, NormalizedBox, TrackObservation


def decision(center=0.5, height=0.1):
    target = TrackObservation(
        DetectionPrediction(1, NormalizedBox(center - 0.05, 0.4, 0.1, height), 0.95),
        ActionPrediction(1, 0.9, 0.0, 0.0),
    )
    return FollowDecision(FollowState.CHASING, 1, target, FollowDecisionReason.TRACKING)


def test_zoom_is_slow_in_and_fast_out() -> None:
    planner = ZoomPlanner(ZoomPlannerConfig(zoom_in_per_second=0.5, zoom_out_per_second=5.0))
    planner.step(decision(), now=0.0, inference_interval_seconds=0.1)
    zoomed = planner.step(decision(0.51), now=1.0, inference_interval_seconds=0.1)
    assert zoomed.scale == 1.5
    wide = planner.step(decision(0.9), now=1.1, inference_interval_seconds=0.1)
    assert wide.scale == 1.0


def test_slow_inference_and_lost_target_use_full_fov() -> None:
    planner = ZoomPlanner(ZoomPlannerConfig())
    planner.step(decision(), now=0.0, inference_interval_seconds=0.1)
    result = planner.step(decision(), now=1.0, inference_interval_seconds=1.0)
    assert result.scale == 1.0
    lost = FollowDecision(FollowState.SEARCHING, None, None, FollowDecisionReason.SEARCHING)
    assert planner.step(lost, now=2.0, inference_interval_seconds=0.1).scale == 1.0
