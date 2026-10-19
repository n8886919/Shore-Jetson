from shore_tracker.inference import IoUTracker, NormalizedBox, RawDetection, box_iou
import pytest


def _detection(x):
    return RawDetection(NormalizedBox(x, 0.2, 0.2, 0.3), 0.9)


def test_iou_and_stable_assignment() -> None:
    assert box_iou(_detection(0.1).box, _detection(0.1).box) == 1
    tracker = IoUTracker(match_iou=0.2)
    first = tracker.update([_detection(0.1), _detection(0.6)])
    second = tracker.update([_detection(0.61), _detection(0.11)])
    assert [item.track_id for item in first] == [1, 2]
    assert [item.track_id for item in second] == [2, 1]


@pytest.mark.parametrize("fps", [5, 10, 20])
def test_track_survives_missing_frames_for_seconds_not_frame_count(fps) -> None:
    tracker = IoUTracker(grace_seconds=5)
    original = tracker.update([_detection(0.1)], now=0)[0].track_id
    for frame in range(1, 5 * fps):
        tracker.update([], now=frame / fps)
    recovered = tracker.update([_detection(0.1)], now=4.99)[0].track_id
    assert recovered == original
    assert tracker.update([_detection(0.1)], now=9.98)[0].track_id == original


@pytest.mark.parametrize("return_at", [5, 5.01, 8])
def test_track_expires_before_matching_even_without_intermediate_frames(return_at):
    tracker = IoUTracker(grace_seconds=5)
    original = tracker.update([_detection(0.1)], now=0)[0].track_id
    replacement = tracker.update([_detection(0.1)], now=return_at)[0].track_id
    assert replacement != original


def test_nondefault_grace_and_injected_monotonic_clock():
    now = [1.0]
    tracker = IoUTracker(grace_seconds=2, clock=lambda: now[0])
    original = tracker.update([_detection(.1)])[0].track_id
    now[0] = 2.99
    assert tracker.update([_detection(.1)])[0].track_id == original
    now[0] = 5.0
    assert tracker.update([_detection(.1)])[0].track_id != original
    with pytest.raises(ValueError, match="monotonic"):
        tracker.update([], now=4)
    tracker.reset()
    assert tracker.update([_detection(.1)], now=0)[0].track_id == 1


@pytest.mark.parametrize("grace", [0, -1, float("nan"), float("inf"), True])
def test_invalid_grace_is_rejected(grace):
    with pytest.raises(ValueError, match="grace_seconds"):
        IoUTracker(grace_seconds=grace)
