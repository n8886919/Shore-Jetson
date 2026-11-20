from dataclasses import replace

import pytest

from shore_tracker.camera import CameraPipeline, PipelineEvent, PipelineEventKind, RtspUri, build_pipeline_spec
from shore_tracker.config import ConfigError, load_config
from shore_tracker.doctor import run_doctor
from shore_tracker.inference import FollowDecision, FollowDecisionReason, FollowState
from shore_tracker.preview import PreviewStore
from shore_tracker.runtime import ShoreTrackerRuntime
from shore_tracker.status import StatusStore
from test_camera_pipeline import FakeFactory
from test_runtime import Camera, Dashboard, Inference, Servo, config


def test_recording_destination_is_explicit_and_checked(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    local = load_config(path)
    assert local.recording.destination == "jetson"
    assert "recording_directory" in run_doctor(local)["checks"]
    path.write_text('[recording]\ndestination = "camera_sd"\n')
    card = load_config(path)
    assert card.recording.destination == "camera_sd"
    assert "recording_directory" not in run_doctor(card)["checks"]
    path.write_text('[recording]\ndestination = "typo"\n')
    with pytest.raises(ConfigError, match="recording.destination"):
        load_config(path)


@pytest.mark.parametrize("codec", ["h264", "h265"])
def test_preview_only_pipeline_tracks_frames_without_recording(codec):
    spec = build_pipeline_spec(codec, None, native_display=True, decoded_inference=True)
    elements = {element.name for element in spec.elements}
    assert {"native_sink", "preview_sink", "inference_sink"} <= elements
    assert elements.isdisjoint({"encoded_tee", "record_queue", "record_parse", "segment_sink"})
    assert all(link.source in elements and link.target in elements for link in spec.links)
    states = StatusStore()
    states.update_component("recording", "unknown", "相機尚未回報卡況")
    recording_before = states.snapshot()["components"]["recording"]
    preview = PreviewStore()
    factory = FakeFactory()
    pipeline = CameraPipeline(status=states, preview=preview, spec=spec, factory=factory,
                              rtsp_uri=RtspUri.from_memory("rtsp://192.0.2.35/live"),
                              on_fragment_closed=lambda _: pytest.fail("Unexpected local video"))
    assert pipeline.start()
    assert pipeline.recording_progress_age_seconds is None
    factory.preview(b"\xff\xd8frame\xff\xd9")
    assert preview.latest() is not None
    assert states.snapshot()["components"]["camera"]["state"] == "ready"
    factory.event(PipelineEvent(PipelineEventKind.ERROR, detail="stream failed"))
    assert states.snapshot()["components"]["camera"]["state"] == "error"
    assert pipeline.stop()
    assert factory.handle.stops == [(False, 5.0)]
    assert states.snapshot()["components"]["recording"] == recording_before


def test_runtime_selects_card_and_keeps_preview_independent_of_local_storage(tmp_path, monkeypatch):
    from shore_tracker.camera.card_recording import CameraCardRecording

    settings = config(tmp_path)
    # Simulate an unavailable local recording disk while protecting an existing local clip.
    archive = tmp_path / "unavailable-mount"
    archive.write_bytes(b"old contents")
    settings = replace(settings, data_dir=archive / "data", recording=replace(
        settings.recording, root=archive / "recordings", destination="camera_sd"))
    monkeypatch.setenv("SHORE_TRACKER_RTSP_URI", "rtsp://192.0.2.8/live")
    monkeypatch.delenv("SHORE_CAMERA_ADMIN_USER", raising=False)
    monkeypatch.delenv("SHORE_CAMERA_ADMIN_PASSWORD", raising=False)

    class PreviewCamera(Camera):
        def start(self):
            self.kwargs["status"].update_component("camera", "ready")
            return True

        @property
        def recording_progress_age_seconds(self):
            pytest.fail("Read a local fragment in camera SD mode")

    runtime = ShoreTrackerRuntime(
        settings, camera_builder=PreviewCamera, dashboard_builder=lambda *_args, **_kw: Dashboard(),
        servo_service_builder=Servo, inference_worker_builder=Inference,
        camera_route_validator=lambda *_args: None, camera_retry_seconds=60,
    )
    try:
        assert isinstance(runtime.manual_recording, CameraCardRecording)
        runtime.start_services()
        first = runtime._camera
        assert first.kwargs["spec"].recording_pattern is None
        assert runtime._storage is None
        assert runtime.controller.arm()[0]
        active_ride = FollowDecision(FollowState.SURFING, 5, None, FollowDecisionReason.TRACKING, True)
        runtime._on_follow_decision(active_ride, 1.0)
        assert not runtime.controller.set_recording_mode(True)[0]
        assert not runtime.manual_recording.snapshot()["tracking_requested"]
        assert runtime.manual_recording.snapshot()["state"] == "unknown"
        runtime.status.update_metrics(disk_pressure="stop")
        runtime._check_camera_once()
        assert runtime._camera is first
        assert runtime._request_camera_reconnect()[0]
        runtime._check_camera_once()
        assert first.stopped and runtime._camera is not first
        assert runtime.diagnostics()["recovery"]["previous_recording_finalized"] is None
    finally:
        runtime.stop_services()
    assert archive.read_bytes() == b"old contents"
