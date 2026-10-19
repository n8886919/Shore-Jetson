from shore_tracker.inference import ActionPrediction, DetectionPrediction, FollowDecisionReason, FollowState, NormalizedBox, TargetFollowConfig, TargetFollowStateMachine, TrackObservation


def observation(track_id=1, *, chasing=0.0, takeoff=0.0, surfing=0.0):
    return TrackObservation(
        DetectionPrediction(track_id, NormalizedBox(0.4, 0.3, 0.1, 0.2), 0.95),
        ActionPrediction(track_id, chasing, takeoff, surfing),
    )


def machine():
    return TargetFollowStateMachine(TargetFollowConfig.from_values(
        chasing_threshold=0.7,
        takeoff_threshold=0.65,
        surfing_threshold=0.7,
        candidate_hold_seconds=0,
        ride_exit_seconds=1.0,
        finished_hold_seconds=1.0,
        stable_observations=1,
    ))


def test_complete_ride_progresses_forward_then_repeats() -> None:
    tracker = machine()
    assert tracker.update([observation(chasing=0.9)], now=0).state is FollowState.CHASING
    takeoff = tracker.update([observation(takeoff=0.8)], now=0.1)
    assert takeoff.state is FollowState.TAKEOFF
    assert takeoff.reason is FollowDecisionReason.ACQUIRED
    surfing = tracker.update([observation(surfing=0.9)], now=0.2)
    assert surfing.state is FollowState.SURFING
    # A classification dip does not regress the phase.
    assert tracker.update([observation(chasing=0.9)], now=0.3).state is FollowState.SURFING
    tracker.update([observation()], now=0.4)
    assert tracker.update([observation()], now=1.5).state is FollowState.FINISHED
    assert tracker.update([], now=2.6).state is FollowState.SEARCHING
    assert tracker.update([observation(2, chasing=0.9)], now=2.7).state is FollowState.CHASING


def test_missing_target_becomes_finished_then_searching() -> None:
    tracker = machine()
    tracker.update([observation(chasing=0.9)], now=0)
    tracker.update([observation(takeoff=0.9)], now=0.05)
    assert tracker.update([], now=0.2).state is FollowState.TAKEOFF
    assert tracker.update([], now=0.6).state is FollowState.TAKEOFF
    assert tracker.update([], now=1.2).state is FollowState.FINISHED
    assert tracker.update([], now=2.3).state is FollowState.SEARCHING


def test_chasing_starts_trial_without_lock_and_no_takeoff_discards():
    tracker = machine()
    trial = tracker.update([observation(4,chasing=.9)],now=0)
    assert trial.state is FollowState.CHASING
    assert trial.target_track_id is None and trial.target is None
    assert not trial.recording_valid
    end = tracker.update([observation(4)],now=1.1)
    assert end.state is FollowState.FINISHED
    assert not end.recording_valid and end.target is None
    assert end.reason is FollowDecisionReason.NO_TAKEOFF


def test_first_takeoff_wins_not_first_chaser_and_target_never_switches():
    tracker = machine()
    tracker.update([observation(1,chasing=.9),observation(2,chasing=.9)],now=0)
    locked = tracker.update([observation(1,chasing=.9),observation(2,takeoff=.8)],now=.1)
    assert locked.target_track_id == 2 and locked.recording_valid
    following = tracker.update([observation(1,takeoff=.99),observation(2,surfing=.8)],now=.2)
    assert following.target_track_id == 2
    assert following.state is FollowState.SURFING


def test_person_never_seen_chasing_cannot_steal_trial_lock():
    tracker = machine()
    tracker.update([observation(1,chasing=.9)],now=0)
    trial=tracker.update([observation(1,chasing=.9),observation(2,takeoff=.99)],now=.1)
    assert trial.target_track_id is None
    trial=tracker.update([observation(2,surfing=.99)],now=.2)
    assert trial.target_track_id is None


def test_return_to_chasing_ends_valid_ride_and_returns_no_pan_target():
    tracker = machine()
    tracker.update([observation(chasing=.9)],now=0)
    tracker.update([observation(takeoff=.9)],now=.1)
    tracker.update([observation(surfing=.9)],now=.2)
    tracker.update([observation(chasing=.99)],now=.3)
    end=tracker.update([observation(chasing=.99)],now=1.4)
    assert end.state is FollowState.FINISHED
    assert end.recording_valid and end.target is None


def permissive_machine():
    return TargetFollowStateMachine(TargetFollowConfig.from_values(
        chasing_threshold=.7, takeoff_threshold=.65, surfing_threshold=.7,
        candidate_hold_seconds=0, stable_observations=1,
        finished_hold_seconds=2, ride_exit_seconds=5,
    ))


def test_five_seconds_from_last_valid_ride_including_mixed_missing_and_low_scores():
    tracker = permissive_machine()
    tracker.update([observation(chasing=.9)], now=0)
    tracker.update([observation(surfing=.9)], now=1)
    assert tracker.update([], now=2).state is FollowState.SURFING
    visible = tracker.update([observation(chasing=.9)], now=4)
    assert visible.state is FollowState.SURFING
    assert visible.target is not None  # Keep following the visible low-score rider.
    assert tracker.update([], now=5.99).state is FollowState.SURFING
    end = tracker.update([], now=6)
    assert end.state is FollowState.FINISHED and end.recording_valid
    assert end.target is None


def test_same_rider_recovery_resets_exit_timer_but_another_rider_does_not():
    tracker = permissive_machine()
    tracker.update([observation(chasing=.9)], now=0)
    tracker.update([observation(surfing=.9)], now=1)
    tracker.update([observation(chasing=.9)], now=2)
    assert tracker.update([observation(surfing=.9)], now=5.99).state is FollowState.SURFING
    assert tracker.update([], now=6).state is FollowState.SURFING
    assert tracker.update([observation(2,surfing=.9)], now=10.98).state is FollowState.SURFING
    end = tracker.update([observation(2,surfing=.9)], now=11)
    assert end.state is FollowState.FINISHED
    assert end.target_track_id == 1


def test_five_second_ride_exit_does_not_change_two_second_trial_or_cooldown():
    tracker = permissive_machine()
    tracker.update([observation(chasing=.9)], now=0)
    tracker.update([observation(surfing=.9)], now=1)
    assert tracker.update([observation(chasing=.9)], now=5.99).state is FollowState.SURFING
    end = tracker.update([], now=6)
    assert end.state is FollowState.FINISHED and end.recording_valid
    assert tracker.update([], now=7.99).state is FollowState.FINISHED
    assert tracker.update([], now=8).state is FollowState.SEARCHING
    tracker.update([observation(chasing=.9)], now=9)
    trial_end = tracker.update([], now=11)
    assert trial_end.state is FollowState.FINISHED
    assert not trial_end.recording_valid


def test_late_same_id_cannot_revive_a_ride_at_the_deadline():
    tracker = permissive_machine()
    tracker.update([observation(chasing=.9)], now=0)
    tracker.update([observation(surfing=.9)], now=1)
    tracker.update([observation(chasing=.9)], now=5.99)
    end = tracker.update([observation(surfing=.9)], now=6)
    assert end.state is FollowState.FINISHED
    assert end.target is None
