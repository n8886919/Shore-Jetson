import time

import pytest

from shore_tracker.preview import PreviewStore


def test_preview_retains_only_latest_complete_jpeg() -> None:
    store = PreviewStore()
    with pytest.raises(ValueError):
        store.publish(b"not jpeg")
    first = b"\xff\xd8one\xff\xd9"
    second = b"\xff\xd8two\xff\xd9"
    store.publish(first, captured_monotonic=time.monotonic())
    sequence = store.latest().sequence  # type: ignore[union-attr]
    store.publish(second, captured_monotonic=time.monotonic())
    latest = store.wait_after(sequence, 0)
    assert latest is not None
    assert latest.jpeg == second


def test_camera_sessions_cannot_publish_frames_across_generations() -> None:
    store = PreviewStore()
    first = store.begin_session()
    first.publish(b"\xff\xd8first\xff\xd9")
    assert store.latest() is not None

    second = store.begin_session()
    assert store.latest() is None
    first.publish(b"\xff\xd8late-first\xff\xd9")
    assert first.latest() is not None
    assert first.latest().jpeg == b"\xff\xd8late-first\xff\xd9"
    assert store.latest() is None

    second.publish(b"\xff\xd8second\xff\xd9")
    assert store.latest() is not None
    assert store.latest().jpeg == b"\xff\xd8second\xff\xd9"
    assert first.latest().jpeg == b"\xff\xd8late-first\xff\xd9"


def test_preview_rate_resets_on_camera_restart_and_ignores_late_generation():
    store = PreviewStore()
    old = store.begin_session()
    jpeg = b'\xff\xd8frame\xff\xd9'
    for i in range(21): old.publish(jpeg,captured_monotonic=10+i*.05)
    assert store.frame_rate(now=11) == pytest.approx(20)
    assert store.frame_rate(now=14) is None
    new = store.begin_session()
    old.publish(jpeg,captured_monotonic=15)
    assert store.frame_rate(now=15) is None
    new.publish(jpeg,captured_monotonic=15)
    new.publish(jpeg,captured_monotonic=15.1)
    assert store.frame_rate(now=15.1) == pytest.approx(10)
