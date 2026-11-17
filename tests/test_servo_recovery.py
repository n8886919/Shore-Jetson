import os

from shore_tracker.servo.simple import SimpleServoClient, _SerialWriter
from shore_tracker.servo.simple_service import ServoService
from test_runtime import recovery_runtime


def make_runtime(tmp_path, monkeypatch, *, online=True):
    state = {'online': online, 'events': []}
    clients = []

    class Writer:
        def __init__(self): self.writes = []
        def open(self):
            state['events'].append('open')
            if not state['online']: raise OSError('USB absent')
        def is_connected(self): return state['online']
        def write(self, payload): self.writes.append(payload)
        def close(self): state['events'].append('close')

    def builder(config, status):
        client = SimpleServoClient(Writer(), angle_min_deg=0, angle_max_deg=180,
                                   protocol='m0_pulse')
        clients.append(client)
        return ServoService(client, status)

    monkeypatch.setattr('shore_tracker.coordinator.TrackingCoordinator.start', lambda self: None)
    runtime = recovery_runtime(tmp_path, monkeypatch)
    runtime._servo_service_builder = builder
    monkeypatch.setattr(runtime, '_start_supervisor', lambda: None)
    return runtime, state, clients


def test_usb_inserted_after_start_recovers_without_restarting_camera(tmp_path, monkeypatch):
    runtime, state, clients = make_runtime(tmp_path, monkeypatch, online=False)
    try:
        runtime.start_services()
        camera = runtime._camera
        assert runtime.status.snapshot()['components']['servo']['state'] == 'error'
        assert state['events'] == ['open', 'close'], 'failed startup must release its writer'
        state['online'] = True
        runtime.controller.recover_servo()
        assert runtime.status.snapshot()['components']['servo']['state'] == 'ready'
        assert runtime._camera is camera and not camera.stopped
        assert not runtime.status.snapshot()['tracking']['armed']
        assert all(client.writer.writes == [] for client in clients)
        assert clients[-1].snapshot().target_degrees is None
    finally:
        runtime.stop_services()


def test_usb_loss_disarms_and_recovery_never_replays_previous_target(tmp_path, monkeypatch):
    runtime, state, clients = make_runtime(tmp_path, monkeypatch)
    try:
        runtime.start_services()
        assert runtime.controller.arm()[0]
        old = clients[-1]
        old.set_target_degrees(95)
        state['online'] = False
        runtime.controller.recover_servo()
        assert runtime.status.snapshot()['tracking']['mode'] == 'disarmed'
        assert not old.snapshot().armed and old.snapshot().target_degrees is None
        state['online'] = True
        runtime.controller.recover_servo()
        new = clients[-1]
        assert new is not old and new.snapshot().ready
        assert not new.snapshot().armed and new.snapshot().target_degrees is None
        new._send_target()
        assert new.writer.writes == []
        assert runtime._camera.stopped is False
        assert state['events'][:3] == ['open', 'close', 'open']
    finally:
        runtime.stop_services()


def test_unreleased_writer_blocks_replacement_and_shutdown_blocks_retry(tmp_path, monkeypatch):
    runtime, state, clients = make_runtime(tmp_path, monkeypatch)
    try:
        runtime.start_services()
        old = runtime._servo
        close = old.close
        monkeypatch.setattr(old, 'close', lambda: False)
        state['online'] = False
        runtime.controller.recover_servo()
        assert runtime._servo is old and len(clients) == 1
        assert not runtime.status.snapshot()['tracking']['armed']
        monkeypatch.setattr(old, 'close', close)
        state['online'] = True
        runtime.controller.recover_servo()
        assert len(clients) == 2
        runtime.stop_services()
        runtime.controller.recover_servo()
        assert len(clients) == 2
    finally:
        runtime.stop_services()


def test_healthy_or_disabled_servo_is_not_reopened(tmp_path, monkeypatch):
    from dataclasses import replace
    runtime, state, clients = make_runtime(tmp_path, monkeypatch)
    try:
        runtime.start_services()
        assert runtime.controller.arm()[0]
        runtime.controller.recover_servo()
        assert len(clients) == 1 and runtime.status.snapshot()['tracking']['armed']
        runtime.config = replace(runtime.config, servo=replace(runtime.config.servo, enabled=False))
        state['online'] = False
        runtime.controller.recover_servo()
        assert len(clients) == 1
    finally:
        runtime.stop_services()


def test_background_supervisor_recovers_late_usb(tmp_path, monkeypatch):
    import threading
    runtime, state, clients = make_runtime(tmp_path, monkeypatch, online=False)
    recovered = threading.Event()
    original = runtime.controller.recover_servo

    def check():
        original()
        if runtime.status.snapshot()['components']['servo']['state'] == 'ready':
            recovered.set()

    monkeypatch.setattr(runtime.controller, 'recover_servo', check)
    runtime._camera_retry_seconds = .01
    try:
        runtime.start_services()
        state['online'] = True
        type(runtime)._start_supervisor(runtime)
        assert recovered.wait(2), 'USB recovery must run without an API call or restart'
        assert not runtime.status.snapshot()['tracking']['armed']
        assert clients[-1].writer.writes == []
    finally:
        runtime.stop_services()


def test_idle_usb_removal_is_detected_without_sending_commands(tmp_path, monkeypatch):
    runtime, state, clients = make_runtime(tmp_path, monkeypatch)
    try:
        runtime.start_services()
        state['online'] = False
        runtime.controller.recover_servo()
        assert runtime.status.snapshot()['components']['servo']['state'] == 'error'
        state['online'] = True
        runtime.controller.recover_servo()
        assert runtime.status.snapshot()['components']['servo']['state'] == 'ready'
        assert all(client.writer.writes == [] for client in clients)
        assert not runtime.status.snapshot()['tracking']['armed']
    finally:
        runtime.stop_services()


def test_arm_queued_during_reopen_does_not_start_tracking(tmp_path, monkeypatch):
    import threading
    runtime, state, clients = make_runtime(tmp_path, monkeypatch)
    entered, release, queued = threading.Event(), threading.Event(), threading.Event()
    result = []
    threads = []
    try:
        runtime.start_services()
        old = runtime._servo
        close = old.close

        def blocked_close():
            entered.set()
            assert release.wait(2)
            return close()

        monkeypatch.setattr(old, 'close', blocked_close)
        state['online'] = False
        recovering = threading.Thread(target=runtime.controller.recover_servo)
        threads.append(recovering); recovering.start()
        assert entered.wait(2)

        # A request waiting for the controller lock has already captured the old
        # recovery epoch. Use a lock wrapper to observe that admission point.
        original_lock = runtime.controller._control_lock
        class ObservedLock:
            def __enter__(self):
                queued.set()
                return original_lock.__enter__()
            def __exit__(self, *args): return original_lock.__exit__(*args)

        monkeypatch.setattr(runtime.controller, '_control_lock', ObservedLock())
        arming = threading.Thread(target=lambda: result.append(runtime.controller.arm()))
        threads.append(arming); arming.start()
        assert queued.wait(2)
        state['online'] = True
        release.set()
        for thread in threads: thread.join(2)
        assert result and not result[0][0]
        assert not runtime.status.snapshot()['tracking']['armed']
        assert clients[-1].writer.writes == []
    finally:
        release.set()
        for thread in threads: thread.join(2)
        runtime.stop_services()


def test_write_failure_latch_recovers_with_fresh_disarmed_client(tmp_path, monkeypatch):
    runtime, state, clients = make_runtime(tmp_path, monkeypatch)
    try:
        runtime.start_services()
        assert runtime.controller.arm()[0]
        old = clients[-1]
        old.set_target_degrees(95)

        def fail(_payload): raise OSError('USB write failed')

        old.writer.write = fail
        old._send_target()
        assert not runtime.status.snapshot()['tracking']['armed']
        runtime.controller.recover_servo()
        new = clients[-1]
        assert new is not old and new.snapshot().ready
        assert not new.snapshot().armed and new.snapshot().target_degrees is None
        assert new.writer.writes == []
    finally:
        runtime.stop_services()


def test_writer_detects_unplug_and_new_device_at_same_path(tmp_path):
    device = tmp_path / 'serial-device'
    device.touch()
    descriptor = os.open(device, os.O_RDWR)

    class Port:
        def fileno(self): return descriptor

    writer = _SerialWriter(device=str(device), baud=9600, protocol='m0_pulse')
    writer.port = Port()
    try:
        assert writer.is_connected()
        device.unlink()
        assert not writer.is_connected()
        device.touch()
        assert not writer.is_connected(), 'a reinserted USB needs a new descriptor'
    finally:
        os.close(descriptor)
