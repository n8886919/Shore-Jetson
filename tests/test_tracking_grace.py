"""Exercise ID continuity, ride policy and clip lifetime together without hardware."""
from dataclasses import replace

import pytest

from shore_tracker.config import TrackingConfig
from shore_tracker.inference import (
    ActionPrediction, FollowState, IoUTracker, NormalizedBox, RawDetection,
    TargetFollowConfig, TargetFollowStateMachine, TrackObservation,
)
from shore_tracker.recording_triggers import RecordingTriggers
from test_recording_triggers import Recorder


@pytest.mark.parametrize('fps', [5, 10, 20])
def test_recovered_rider_keeps_id_and_one_clip_until_shared_deadline(fps):
    grace = TrackingConfig().ride_exit_seconds
    ids = IoUTracker(grace_seconds=grace)
    machine = TargetFollowStateMachine(TargetFollowConfig(
        selector=replace(TargetFollowConfig().selector, stable_observations=1, stable_seconds=0),
        ride_exit_seconds=grace,
    ))
    recorder = Recorder()
    now = [0.0]
    triggers = RecordingTriggers(recorder, armed_reader=lambda: True, clock=lambda: now[0])
    raw = RawDetection(NormalizedBox(.4, .2, .2, .3), .9)

    def frame(at, action=None):
        now[0] = at
        detections = ids.update([raw] if action else [], now=at)
        observations = tuple(TrackObservation(d, ActionPrediction(
            d.track_id, .9 if action == 'chase' else 0, 0, .9 if action == 'surf' else 0,
        )) for d in detections)
        decision = machine.update(observations, now=at)
        triggers.follow(decision, at)
        triggers.check_once()
        return decision

    frame(0, 'chase')
    locked = frame(1, 'surf')
    for tick in range(1, int(4.8 * fps)):
        decision = frame(1 + tick / fps)
        assert decision.state is FollowState.SURFING
        assert recorder.state == 'recording'
    recovered = frame(5.8, 'surf')
    assert recovered.target_track_id == locked.target_track_id
    assert recovered.target is not None
    # Another five seconds starts at recovery, with no extra delay for noticing loss.
    for tick in range(1, 5 * fps):
        decision = frame(5.8 + tick / fps)
        assert decision.state is FollowState.SURFING
        assert recorder.state == 'recording'
    ended = frame(10.8)
    assert ended.state is FollowState.FINISHED and ended.recording_valid
    assert [action for action, _ in recorder.calls] == ['start', 'stop']
    assert recorder.state == 'finalizing'
