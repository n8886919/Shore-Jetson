from threading import Event

from shore_tracker.inference.frames import DecodedFrames
from shore_tracker.inference.worker import InferenceWorker
from shore_tracker.status import StatusStore


def test_latest_generation_and_rgba_stride():
    frames = DecodedFrames()
    old = frames.begin_session()
    old(bytes([255, 0, 0, 255, 0, 0, 0, 0]), 1, 1, 8)
    frame = frames.wait_after(0, 0)
    assert frame.to_image().getpixel((0, 0)) == (255, 0, 0)
    publish = frames.begin_session(camera_generation=7)
    old(bytes(8), 1, 1, 8)
    assert frames.wait_after(0, 0) is None
    for n in range(20):
        publish(bytes([n, 0, 0, 255]), 1, 1, 4, source_pts_ns=100+n)
    assert frames.wait_after(0, 0).to_image().getpixel((0, 0)) == (19, 0, 0)
    assert frames.wait_after(0, 0).generation == 7
    assert frames.wait_after(0, 0).source_pts_ns == 119


def test_decoded_worker_bypasses_jpeg():
    frames = DecodedFrames()
    publish = frames.begin_session()
    publish(bytes([12, 34, 56, 255]), 1, 1, 4)
    done = Event()

    class Models:
        def load(self): pass
        def infer_image(self, image):
            assert image.getpixel((0, 0)) == (12, 34, 56)
            return ()
        def infer_jpeg(self, jpeg):
            raise AssertionError('decoded input must not pass through JPEG')

    class Coordinator:
        def process_frame(self, observations, *, now): done.set()

    worker = InferenceWorker(preview=frames, status=StatusStore(), models=Models(),
                             coordinator=Coordinator(), stale_seconds=1.5)
    try:
        worker.start()
        assert done.wait(1)
    finally:
        assert worker.close(timeout=1)
