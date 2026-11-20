from dataclasses import replace
import pytest
from shore_tracker.config import AppConfig, RecordingConfig, ServoConfig
from shore_tracker.runtime import ShoreTrackerRuntime


class Dashboard:
    def serve_forever(self, **_kwargs): pass
    def shutdown(self): pass
    def server_close(self): pass


class Camera:
    instances = []
    terminal_kind = None
    progress_age_seconds = 0.0
    recording_progress_age_seconds = 0.0

    def __init__(self, **kwargs):
        self.__class__.instances.append(self)
        self.kwargs = kwargs
        self.stopped = False

    def start(self):
        self.kwargs["status"].update_component("camera", "ready")
        return True

    def stop(self, *, timeout_seconds):
        self.stopped = True
        return True


class Servo:
    def __init__(self, _config, status): self.status = status; self.targets = []
    def start(self): self.status.update_component("servo", "ready")
    def request_arm(self): self.status.confirm_armed(); return True, "armed"
    def set_target_degrees(self, value): self.targets.append(value)
    def stop_motion(self, _reason=""): pass
    def close(self, **_kwargs): return True


class Inference:
    def __init__(self, **kwargs): self.status = kwargs["status"]
    def start(self): self.status.update_component("inference", "ready")
    def close(self, **_kwargs): return True


def config(tmp_path):
    return replace(
        AppConfig(),
        data_dir=tmp_path / "data",
        recording=replace(RecordingConfig(), root=tmp_path / "recordings", free_warning_gib=2, free_stop_gib=1),
        servo=replace(ServoConfig(), pan_direction=1),
    )


@pytest.mark.parametrize("codec", ["h264", "h265"])
def test_start_and_reconnect_never_record_in_background(tmp_path, monkeypatch, codec):
    Camera.instances.clear()
    monkeypatch.setenv("SHORE_TRACKER_RTSP_URI", "rtsp://user:secret@192.0.2.8/live")
    settings = config(tmp_path)
    settings.recording.root.mkdir(parents=True)
    old_video = settings.recording.root / "existing.mkv"
    old_video.write_bytes(b"existing footage")
    runtime = ShoreTrackerRuntime(
        replace(settings, camera=replace(settings.camera, codec=codec, preview_fps=13)),
        dashboard_builder=lambda *_args, **_kwargs: Dashboard(),
        camera_builder=Camera,
        servo_service_builder=Servo,
        inference_worker_builder=Inference,
        camera_route_validator=lambda *_args: None,
        camera_retry_seconds=60,
    )
    runtime.manual_recording.recorder.builder = lambda *_args: pytest.fail("Unexpected recording")
    try:
        runtime.start_services()
        camera = runtime._camera
        assert dict(camera.kwargs["spec"].element("preview_caps").properties)["caps"] == "video/x-raw,framerate=13/1"
        # Missing fragment progress is normal for a preview-only camera.
        camera.recording_progress_age_seconds = 1000
        runtime._check_camera_once()
        assert runtime._camera is camera and not camera.stopped
        assert runtime.controller.reconnect_camera()[0]
        runtime._check_camera_once()
        assert camera.stopped and runtime._camera is not camera
        # An error recovery also rebuilds only the preview branch.
        runtime._camera.terminal_kind = "error"
        runtime._check_camera_once()
        for instance in Camera.instances:
            spec = instance.kwargs["spec"]
            assert spec.recording_pattern is None
            assert {e.factory for e in spec.elements}.isdisjoint({"splitmuxsink", "filesink"})
        runtime.manual_recording.check_once()
        assert runtime.manual_recording.snapshot()["state"] == "idle"
        assert runtime.status.snapshot()["metrics"]["last_recording"] is None
        assert runtime.status.snapshot()["components"]["recording"]["state"] == "ready"
        assert list(settings.recording.root.iterdir()) == [old_video]
        assert old_video.read_bytes() == b"existing footage"
        assert not list(settings.data_dir.rglob("*.mkv"))
    finally:
        runtime.stop_services()


def test_real_runtime_wires_target_acquisition_to_completed_uploadable_clip(tmp_path, monkeypatch):
    import json
    import time
    from shore_tracker.inference import ActionPrediction, DetectionPrediction, NormalizedBox, TrackObservation
    from test_manual_recording import Clip
    from test_coordinator import process_and_control
    monkeypatch.setattr('shore_tracker.coordinator.TrackingCoordinator.start', lambda self: None)
    monkeypatch.setenv('SHORE_TRACKER_RTSP_URI','rtsp://test:secret@192.0.2.8/live')
    runtime = ShoreTrackerRuntime(
        replace(config(tmp_path), servo=replace(ServoConfig(),pan_direction=-1)),
        dashboard_builder=lambda *_args, **_kwargs: Dashboard(), camera_builder=Camera,
        servo_service_builder=Servo, inference_worker_builder=Inference,
        camera_route_validator=lambda *_args: None, camera_retry_seconds=60,
    )
    runtime.manual_recording.recorder.builder = Clip
    try:
        runtime.start_services()
        assert runtime.controller.arm()[0]
        sample = TrackObservation(DetectionPrediction(1,NormalizedBox(.7,.3,.1,.2),.95),ActionPrediction(1,.1,.1,.9))
        started = time.monotonic()
        chaser=TrackObservation(sample.detection,ActionPrediction(1,.9,.1,.1))
        for offset in (0,.25,.5):
            process_and_control(runtime._coordinator, [chaser], now=started+offset)
        runtime.manual_recording.check_once()
        assert runtime._servo.targets[-1] == 90
        for offset in (.75,1,1.25):
            process_and_control(runtime._coordinator, [sample], now=started+offset)
        runtime.manual_recording.check_once()
        assert runtime._servo.targets[-1] < 90
        assert runtime.manual_recording.snapshot()['tracking_requested']
        assert not runtime.manual_recording.snapshot()['manual_requested']
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and runtime.manual_recording.snapshot()['state']!='recording':time.sleep(.01)
        process_and_control(runtime._coordinator, [], now=started+3.0)
        runtime.manual_recording.check_once()
        held_angle = runtime._servo.targets[-1]
        process_and_control(runtime._coordinator, [], now=started+6.24)
        runtime.manual_recording.check_once()
        assert runtime.manual_recording.snapshot()['tracking_requested']
        assert runtime._servo.targets[-1] == held_angle
        process_and_control(runtime._coordinator, [], now=started+6.25)
        runtime.manual_recording.check_once()
        assert not runtime.manual_recording.snapshot()['tracking_requested']
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and not runtime.manual_recording.recordings():time.sleep(.01)
        clips=runtime.manual_recording.recordings()
        assert len(clips)==1
        assert clips[0]['state']=='ready'
        assert clips[0]['triggers']==['tracking']
        assert clips[0]['target_track_ids']==[1]
        assert runtime.manual_recording.file(clips[0]['id']).read_bytes()==b'encoded-test-video'
        assert runtime._camera.stopped is False
    finally:runtime.stop_services()


def test_runtime_passes_one_grace_setting_to_tracker_and_follow_policy(tmp_path):
    captured = []

    class CaptureInference(Inference):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured.append(kwargs)

    base = config(tmp_path)
    runtime = ShoreTrackerRuntime(
        replace(base, tracking=replace(base.tracking, ride_exit_seconds=7)),
        dashboard_builder=lambda *_args, **_kwargs: Dashboard(),
        servo_service_builder=Servo, inference_worker_builder=CaptureInference,
    )
    runtime._start_inference()
    assert captured[0]['models'].tracker.grace_seconds == 7
    assert captured[0]['coordinator'].machine.config.ride_exit_seconds == 7
    assert captured[0]['stale_seconds'] == 1.5


def recovery_runtime(tmp_path, monkeypatch, camera_builder=Camera):
    monkeypatch.setenv('SHORE_TRACKER_RTSP_URI', 'rtsp://user:secret@192.0.2.8/live')
    return ShoreTrackerRuntime(
        config(tmp_path), dashboard_builder=lambda *a, **k: Dashboard(),
        camera_builder=camera_builder, servo_service_builder=Servo,
        inference_worker_builder=Inference, camera_route_validator=lambda *a: None,
        camera_retry_seconds=60,
    )


def test_recovery_after_error_requires_resource_release_but_not_complete_recording(tmp_path, monkeypatch):
    runtime = recovery_runtime(tmp_path, monkeypatch)
    try:
        runtime.start_services()
        old = runtime._camera
        old.terminal_kind = 'error'
        old.stop = lambda **kwargs: False
        old.resources_released = False
        runtime._check_camera_once()
        assert runtime._camera is old
        assert runtime.diagnostics()['recovery']['state'] == 'blocked'
        old.resources_released = True
        runtime._check_camera_once()
        assert runtime._camera is not old
        assert runtime.diagnostics()['recovery']['previous_recording_finalized'] is None
        assert not runtime.status.snapshot()['tracking']['armed']
        assert runtime._camera.kwargs['spec'].recording_pattern is None
    finally:
        runtime.stop_services()


def test_manual_reconnect_is_queued_guarded_and_keeps_servo_disarmed(tmp_path, monkeypatch):
    runtime = recovery_runtime(tmp_path, monkeypatch)
    try:
        runtime.start_services()
        assert runtime.controller.arm()[0]
        assert not runtime.controller.reconnect_camera()[0]
        runtime.controller.stop()
        old = runtime._camera
        assert runtime.controller.reconnect_camera()[0]
        assert runtime._camera is old and not old.stopped
        assert not runtime.controller.reconnect_camera()[0]
        # A late readiness callback cannot reopen the ARM/recording gate.
        runtime.status.update_component('camera', 'ready')
        assert not runtime.controller.arm()[0]
        assert not runtime.controller.recording_command('start')[0]
        runtime._check_camera_once()
        assert old.stopped and runtime._camera is not old
        assert not runtime.status.snapshot()['tracking']['armed']
        assert runtime._servo.targets == []
        diagnostic = runtime.diagnostics()
        assert diagnostic['recovery']['last_reason'] == 'manual'
        assert 'rtsp://' not in str(diagnostic) and 'secret' not in str(diagnostic)
    finally:
        runtime.stop_services()


def test_failed_start_retains_unreleased_camera_and_shutdown_does_not_restart(tmp_path, monkeypatch):
    class FailedCamera(Camera):
        resources_released = False
        terminal_kind = 'error'
        def start(self): return False
        def stop(self, **kwargs): return False
    runtime = recovery_runtime(tmp_path, monkeypatch, FailedCamera)
    runtime._start_camera()
    old = runtime._camera
    assert old is not None
    runtime._check_camera_once()
    assert runtime._camera is old
    runtime._supervisor_stop.set()
    old.resources_released = True
    runtime._check_camera_once()
    assert runtime._camera is old


def test_storage_pressure_prevents_automatic_reconnect(tmp_path, monkeypatch):
    runtime = recovery_runtime(tmp_path, monkeypatch)
    runtime.status.update_metrics(disk_pressure='stop')
    runtime._check_camera_once()
    assert runtime._camera is None
