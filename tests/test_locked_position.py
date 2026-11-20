import numpy as np
from types import SimpleNamespace
from unittest.mock import Mock

from shore_tracker.inference.frames import DecodedFrame, DecodedFrames
from shore_tracker.inference.position import LockedPosition
from shore_tracker.inference.tracker import IoUTracker, RawDetection
from shore_tracker.inference.types import NormalizedBox


TEXTURE = np.random.default_rng(41).integers(30, 240, size=(24, 12), dtype=np.uint8)


def frame(i, x, blank=False, duplicate=False, generation=1):
    pixels = np.zeros((270, 480, 4), dtype=np.uint8)
    if not blank:
        pixels[90:114, x:x+12, 1] = TEXTURE
        if duplicate:
            pixels[90:114, x+15:x+27, 1] = TEXTURE
    return DecodedFrame(i+1, .1*i, pixels.tobytes(), 480, 270, 1920,
                        generation, i*100_000_000)


def setup():
    box = NormalizedBox(100/480, 90/270, 12/480, 24/270)
    coord = Mock()
    coord.process_frame.return_value = SimpleNamespace(target=SimpleNamespace(
        detection=SimpleNamespace(box=box)), target_track_id=1)
    status = Mock()
    status.inference_run.return_value = (4, 0)
    frames = DecodedFrames()
    frames.begin_session(camera_generation=1)
    position = LockedPosition(frames, coord, status, 1.5, lambda: .4)
    position.detected(frame(0, 100), (4, 0), (), 0)
    return position, coord, box


def test_motion_keeps_id_across_nonoverlapping_detections():
    p, c, box = setup()
    p.advance(frame(1, 110))
    p.advance(frame(2, 120))
    hint = p.hint(frame(3, 130), (4, 0))
    assert hint and abs(hint[1].x - 130/480) < .003
    detection = RawDetection(NormalizedBox(130/480, box.y, box.width, box.height), .9)
    for supplied, expected in [(None, 2), (hint, 1)]:
        tracker = IoUTracker(match_iou=.25)
        tracker.update([RawDetection(box, .9)], now=0)
        assert tracker.update([detection], now=.3, position_hint=supplied)[0].track_id == expected
    assert c.process_frame.call_count == 1  # no repeated action confirmations


def test_ambiguous_blank_or_reconnected_view_holds_control():
    for f in (frame(1,103,blank=True),frame(1,100,duplicate=True),frame(1,103,generation=2)):
        p, c, _ = setup()
        p.advance(f)
        c.update_locked_position.assert_called_once_with(4, 1, None)
        assert p.hint(frame(2,105), (4,0)) is None


def test_old_result_run_and_time_bounds():
    p, c, _ = setup()
    p.advance(frame(2,115))
    assert p.hint(frame(1,107), (4,0)) is not None
    p.advance(frame(1,107))
    assert c.update_locked_position.call_count == 1
    assert p.hint(frame(3,120), (5,0)) is None
    p.clock = lambda: 2
    assert p.hint(frame(3,120), (4,0)) is None
