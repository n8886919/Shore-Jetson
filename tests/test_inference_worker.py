from types import SimpleNamespace
from threading import Event

from shore_tracker.inference.worker import InferenceWorker
from shore_tracker.status import StatusStore


class Preview:
    def __init__(self) -> None:
        self.calls = 0

    def wait_after(self, _sequence, timeout):
        self.calls += 1
        if self.calls <= 2:
            return SimpleNamespace(sequence=self.calls, jpeg=b"jpeg")
        Event().wait(timeout)
        return None


class Models:
    def load(self) -> None:
        pass

    def infer_jpeg(self, _jpeg):
        return ()


class Coordinator:
    def __init__(self) -> None:
        self.completed = Event()
        self.calls = 0

    def process_frame(self, _observations, *, now):
        self.calls += 1
        if self.calls == 2:
            self.completed.set()


class Clock:
    def __init__(self) -> None:
        self.values = iter((0.0, 0.0, 0.0, 0.1, 0.1, 0.1, 0.3))
        self.last = 0.3

    def __call__(self) -> float:
        try:
            self.last = next(self.values)
        except StopIteration:
            self.last += 0.1
        return self.last


def test_reported_fps_is_completed_frame_rate_not_model_throughput() -> None:
    status = StatusStore()
    coordinator = Coordinator()
    worker = InferenceWorker(
        preview=Preview(),
        status=status,
        coordinator=coordinator,
        models=Models(),
        stale_seconds=1.0,
        clock=Clock(),
    )

    worker.start()
    assert coordinator.completed.wait(1.0)
    assert worker.close(timeout=1.0)

    assert status.snapshot()["metrics"]["inference_fps"] == 5.0


def test_slow_inference_does_not_block_preview_and_skips_old_frames():
    from shore_tracker.preview import PreviewStore
    import time
    store = PreviewStore()
    started, release, completed = Event(), Event(), Event()
    received=[]
    class SlowModels:
        def load(self): pass
        def infer_jpeg(self,jpeg):
            received.append(jpeg)
            if len(received)==1:
                started.set()
                if not release.wait(2): raise RuntimeError('test inference timed out')
            return ()
    class Sink:
        def process_frame(self,observations,*,now):
            if len(received)==2: completed.set()
    first=b'\xff\xd8first\xff\xd9'
    store.publish(first)
    worker=InferenceWorker(preview=store,status=StatusStore(),coordinator=Sink(),
        models=SlowModels(),stale_seconds=1.5)
    worker.start()
    try:
        assert started.wait(1)
        initial=store.latest().sequence
        for i in range(20):
            latest=b'\xff\xd8'+str(i).encode()+b'\xff\xd9'
            store.publish(latest)
        assert store.wait_after(initial,0).jpeg == latest
        assert received == [first]  # Model still blocked; preview has advanced.
        release.set()
        assert completed.wait(1)
        assert received == [first,latest]  # No queue of the 19 skipped frames.
    finally:
        release.set()
        assert worker.close(timeout=1)


def test_explicit_two_hz_uses_latest_frames_without_throttling_preview():
    import time
    from threading import Thread
    from shore_tracker.preview import PreviewStore
    store=PreviewStore();status=StatusStore();done=Event();stop=Event();calls=[];received=[]
    class Model:
        def load(self): pass
        def infer_jpeg(self,jpeg): calls.append(time.monotonic());received.append(jpeg);return ()
    class Sink:
        def process_frame(self,observations,*,now):
            if len(calls)>=3:done.set()
    def produce():
        index=0
        while not stop.is_set():
            store.publish(b'\xff\xd8'+str(index).encode()+b'\xff\xd9');index+=1;stop.wait(.01)
    producer=Thread(target=produce);producer.start()
    worker=InferenceWorker(preview=store,status=status,coordinator=Sink(),models=Model(),stale_seconds=1.5,max_fps=2)
    try:
        assert worker.max_fps==2
        worker.start();assert done.wait(3)
        assert len(calls)==3
        assert all(b-a>=.49 for a,b in zip(calls,calls[1:]))
        assert store.latest().sequence>20
        assert len(set(received))==3
        assert 1.5<status.snapshot()['metrics']['inference_fps']<=2.1
    finally:
        stop.set();producer.join(1);assert worker.close(timeout=.3)


def test_rate_change_wakes_waiter_and_stop_does_not_wait_for_rate_period():
    import time
    from shore_tracker.preview import PreviewStore
    store=PreviewStore();first=Event();second=Event();calls=[]
    class Model:
        def load(self):pass
        def infer_jpeg(self,jpeg):calls.append(jpeg);return ()
    class Sink:
        def process_frame(self,observations,*,now):
            (first if len(calls)==1 else second).set()
    store.publish(b'\xff\xd8first\xff\xd9')
    worker=InferenceWorker(preview=store,status=StatusStore(),coordinator=Sink(),models=Model(),stale_seconds=1.5,max_fps=1)
    try:
        worker.start();assert first.wait(1)
        store.publish(b'\xff\xd8latest\xff\xd9')
        worker.set_max_fps(10)
        assert second.wait(.5)
        assert calls[-1]==b'\xff\xd8latest\xff\xd9'
        worker.set_max_fps(1)
        assert worker.close(timeout=.25)
    finally:worker.close(timeout=1)


def test_invalid_rate_cannot_replace_current_rate():
    import pytest
    worker=InferenceWorker(preview=Preview(),status=StatusStore(),coordinator=Coordinator(),models=Models(),stale_seconds=1.5)
    for value in (-1,.5,11,float('nan'),float('inf'),True,'2',None):
        with pytest.raises(ValueError):worker.set_max_fps(value)
        assert worker.max_fps==0


def test_on_demand_does_no_inference_until_arm_and_discards_inflight_stop():
    import time
    from shore_tracker.preview import PreviewStore
    store = PreviewStore()
    status = StatusStore()
    for component in ('camera', 'servo'):
        status.update_component(component, 'ready')
    entered, release, completed = Event(), Event(), Event()
    calls, published = [], []
    class Model:
        def load(self): pass
        def infer_jpeg(self, jpeg):
            calls.append(jpeg)
            entered.set()
            assert release.wait(2)
            return ()
    class Sink:
        def process_frame(self, observations, **kwargs): published.append(observations)
    worker = InferenceWorker(preview=store,status=status,coordinator=Sink(),models=Model(),
                             stale_seconds=1.5,on_demand=True,max_fps=10)
    old = b'\xff\xd8before-arm\xff\xd9'
    store.publish(old)
    worker.start()
    try:
        deadline = time.monotonic() + 1
        while status.snapshot()['components']['inference']['state'] != 'idle' and time.monotonic() < deadline:
            time.sleep(.01)
        assert status.can_arm()[0]
        assert not entered.wait(.15)
        assert status.arm()[0]
        assert not entered.wait(.1)  # Pre-ARM images cannot become a target.
        store.publish(b'\xff\xd8after-arm\xff\xd9')
        assert entered.wait(1)
        status.disarm('test stop')
        # Even immediate re-ARM cannot consume the old run's GPU result.
        assert status.arm()[0]
        release.set()
        time.sleep(.15)
        assert published == []
        status.disarm('done')
        assert len(calls) == 1
    finally:
        release.set()
        assert worker.close(timeout=1)


def test_default_worker_does_not_wait_between_available_frames():
    import time
    coordinator = Coordinator()
    worker = InferenceWorker(preview=Preview(), status=StatusStore(),
                             coordinator=coordinator, models=Models(), stale_seconds=1.5)
    assert worker.max_fps == 0
    started = time.monotonic()
    worker.start()
    try:
        assert coordinator.completed.wait(.35)
        assert time.monotonic() - started < .35
    finally:
        assert worker.close(timeout=.3)
