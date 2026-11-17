from shore_tracker.display import DisplayFrames


def test_exact_pts_annotations_and_plain_frames_are_played_in_order():
    now = [10.0]
    frames = DisplayFrames(clock=lambda: now[0])
    frames.begin_session(1)
    frames.publish(1, 0, b'plain0')
    now[0] = 10.1
    frames.publish(1, 100_000_000, b'plain1')
    now[0] = 10.5
    assert frames.publish(1, 0, b'inferred0', run_id=4, recognition={'detections': ['a']})
    assert frames.wait_after(0, timeout=0) is None
    now[0] = 11.0
    first = frames.wait_after(0, timeout=0)
    assert first.jpeg == b'inferred0'
    assert first.run_id == 4
    now[0] = 11.11
    second = frames.wait_after(first.timestamp, timeout=0)
    assert second.jpeg == b'plain1'
    assert second.recognition is None  # Never reuse frame zero's boxes.


def test_late_results_and_old_camera_generations_cannot_replace_display():
    now = [20.0]
    frames = DisplayFrames(clock=lambda: now[0])
    frames.begin_session(1)
    frames.publish(1, 0, b'first')
    now[0] = 21.01
    assert not frames.publish(1, 0, b'late', recognition={'detections': ['wrong']})
    assert frames.wait_after(0, timeout=0).jpeg == b'first'
    frames.begin_session(2)
    assert not frames.publish(1, 10, b'old-session')
    assert frames.wait_after(0, timeout=0) is None


def test_raw_inference_pts_absent_from_preview_is_inserted_at_its_own_time():
    now = [30.0]
    frames = DisplayFrames(clock=lambda: now[0])
    frames.begin_session(1)
    frames.publish(1, 0, b'preview')
    now[0] = 30.7
    frames.publish(1, 250_000_000, b'exact-raw', run_id=1, recognition={})
    now[0] = 31.26
    frame = frames.wait_after(30.0, timeout=0)
    assert frame.jpeg == b'exact-raw'
    assert frame.timestamp == 30.25


def test_buffer_is_bounded_and_rejects_unknown_timestamps():
    frames = DisplayFrames(clock=lambda: 1)
    frames.begin_session(1)
    assert not frames.publish(1, None, b'unknown')
    assert not frames.publish(1, 2**64-1, b'unknown')
    for pts in range(100):
        frames.publish(1, pts, b'jpeg')
    assert len(frames._frames) <= 48
