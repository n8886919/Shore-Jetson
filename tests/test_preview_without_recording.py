from dataclasses import replace
import pytest
from shore_tracker.camera import CameraPipeline, PipelineEvent, PipelineEventKind, RtspUri, build_pipeline_spec
from shore_tracker.preview import PreviewStore
from shore_tracker.status import StatusStore
from test_camera_pipeline import FakeFactory

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
