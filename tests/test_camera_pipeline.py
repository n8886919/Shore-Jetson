from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from shore_tracker.camera import (
    CameraPipeline,
    ClosedFragment,
    GStreamerPipelineFactory,
    GStreamerUnavailable,
    PipelineEvent,
    PipelineEventKind,
    OpenedFragment,
    RtspUri,
    VideoCodec,
    build_pipeline_spec,
)
from shore_tracker.preview import PreviewStore
from shore_tracker.status import StatusStore
from shore_tracker.camera.pipeline import _FragmentBoundaryTracker, _GstPipelineHandle


SECRET_URI = "rtsp://camera-user:camera-password@192.0.2.20/private/live"


def test_gstreamer_jpeg_delivery_preserves_source_pts():
    from types import SimpleNamespace
    received=[]
    buffer=SimpleNamespace(pts=123456789,get_size=lambda:4,extract_dup=lambda *_:b'jpeg')
    sample=SimpleNamespace(get_buffer=lambda:buffer)
    sink=SimpleNamespace(emit=lambda _:sample)
    handle=object.__new__(_GstPipelineHandle)
    handle._gst=SimpleNamespace(FlowReturn=SimpleNamespace(OK='ok',EOS='eos',ERROR='error'))
    handle._on_preview_jpeg=lambda jpeg, **metadata: received.append((jpeg,metadata))
    assert handle._new_sample(sink) == 'ok'
    assert received == [(b'jpeg',{'source_pts_ns':123456789})]


def test_sample_timing_reaches_the_preview_through_the_camera_adapter(tmp_path):
    from types import SimpleNamespace
    factory=FakeFactory()
    adapter,status,preview=make_adapter(tmp_path,factory)
    assert adapter.start()
    buffer=SimpleNamespace(pts=1_000_000_000,get_size=lambda:7,extract_dup=lambda *_:b'\xff\xd8jpg\xff\xd9')
    sample=SimpleNamespace(get_buffer=lambda:buffer,get_segment=lambda:SimpleNamespace(to_running_time=lambda *_:1_000_000_000))
    handle=object.__new__(_GstPipelineHandle)
    handle._gst=SimpleNamespace(Format=SimpleNamespace(TIME='time'),FlowReturn=SimpleNamespace(OK='ok',EOS='eos',ERROR='error'))
    handle._pipeline=SimpleNamespace(get_clock=lambda:SimpleNamespace(get_time=lambda:8_000_000_000),get_base_time=lambda:2_000_000_000)
    handle._on_preview_jpeg=factory.on_preview
    assert handle._new_sample(SimpleNamespace(emit=lambda _:sample))=='ok'
    assert preview.latest().pipeline_delay_ms==5000
    assert preview.latest().source_pts_ns==1_000_000_000
    assert status.snapshot()['components']['camera']['state']=='ready'


def properties(spec, element_name: str) -> dict[str, object]:
    return dict(spec.element(element_name).properties)


def test_decoded_inference_keeps_encoded_recording_and_preview_independent(tmp_path):
    original = build_pipeline_spec('h265', tmp_path / 'clip-%05d.mkv')
    raw = build_pipeline_spec('h265', tmp_path / 'clip-%05d.mkv', decoded_inference=True)
    for name in ('record_queue', 'record_parse', 'segment_sink', 'preview_caps'):
        assert properties(original, name) == properties(raw, name)
    assert properties(raw, 'inference_rate_caps')['caps'] == 'video/x-raw(memory:NVMM),framerate=4/1'
    assert properties(raw, 'inference_raw_caps')['caps'] == 'video/x-raw,format=RGBA'
    assert properties(raw, 'inference_queue')['leaky'] == 'downstream'
    assert len([e for e in raw.elements if e.factory == 'nvv4l2decoder']) == 1


@pytest.mark.parametrize(
    ("codec", "encoding", "depay", "parser"),
    [
        (VideoCodec.H264, "H264", "rtph264depay", "h264parse"),
        (VideoCodec.H265, "H265", "rtph265depay", "h265parse"),
    ],
)
def test_spec_is_explicit_video_only_and_uri_free(
    tmp_path: Path,
    codec: VideoCodec,
    encoding: str,
    depay: str,
    parser: str,
) -> None:
    spec = build_pipeline_spec(codec, tmp_path / "session-%05d.mkv")

    factories = {element.factory for element in spec.elements}
    assert spec.expected_rtp_media == "video"
    assert spec.expected_rtp_encoding == encoding
    assert spec.rtsp_transport == "tcp"
    assert spec.audio_enabled is False
    assert depay in factories
    assert parser in factories
    assert "nvv4l2decoder" in factories
    assert "nvvidconv" in factories
    assert "avdec_h264" not in factories
    assert "avdec_h265" not in factories
    assert not any(factory.startswith("rtpmp4a") for factory in factories)
    assert not any("audio" in element.name.lower() for element in spec.elements)
    assert not any(
        "audio" in f"{link.source} {link.target}".lower() for link in spec.links
    )
    assert SECRET_URI not in repr(spec)
    assert properties(spec, "rtsp_source")["protocols"] == "tcp"


def test_recording_branch_is_matroska_ten_minutes_and_never_prunes(
    tmp_path: Path,
) -> None:
    spec = build_pipeline_spec("h264", tmp_path / "session-%05d.mkv")
    sink = properties(spec, "segment_sink")

    assert sink["muxer-factory"] == "matroskamux"
    assert sink["max-size-time"] == 600_000_000_000
    assert sink["max-size-bytes"] == 0
    assert sink["max-files"] == 0
    assert sink["send-keyframe-requests"] is True
    record_link = next(link for link in spec.links if link.target == "segment_sink")
    assert record_link.target_pad == "video"
    assert record_link.request_target is True


def test_preview_branch_is_bounded_and_low_latency(tmp_path: Path) -> None:
    spec = build_pipeline_spec("h265", tmp_path / "session-%05d.mkv")

    queue = properties(spec, "preview_queue")
    raw_caps = properties(spec, "preview_raw_caps")
    caps = properties(spec, "preview_caps")
    sink = properties(spec, "preview_sink")
    assert queue["leaky"] == "downstream"
    assert queue["max-size-buffers"] == 2
    assert raw_caps["caps"] == "video/x-raw,width=960,height=540"
    assert caps["caps"] == "video/x-raw,framerate=20/1"
    assert sink["max-buffers"] == 1
    assert sink["drop"] is True
    assert sink["sync"] is False
    assert sink["wait-on-eos"] is False


def test_upside_down_camera_rotates_decoded_frames_before_jpeg(tmp_path: Path) -> None:
    normal = build_pipeline_spec("h265", tmp_path / "session-%05d.mkv")
    upright = build_pipeline_spec(
        "h265", tmp_path / "session-%05d.mkv", rotation_degrees=180,
    )
    assert properties(normal, "preview_nvconvert")["flip-method"] == 0
    assert properties(upright, "preview_nvconvert")["flip-method"] == 2
    assert upright.links == normal.links
    assert upright.element("segment_sink") == normal.element("segment_sink")


class FakeCapsStructure:
    def __init__(self, media: str, encoding: str) -> None:
        self.values = {"media": media, "encoding-name": encoding}

    def get_string(self, name: str) -> str | None:
        return self.values.get(name)


class FakeCaps:
    def __init__(self, media: str, encoding: str) -> None:
        self.structure = FakeCapsStructure(media, encoding)

    def get_size(self) -> int:
        return 1

    def get_structure(self, _index: int) -> FakeCapsStructure:
        return self.structure


class FakePad:
    def __init__(self, media: str, encoding: str) -> None:
        self.caps = FakeCaps(media, encoding)
        self.linked_to = None

    def get_current_caps(self) -> FakeCaps:
        return self.caps

    def link(self, other) -> int:
        self.linked_to = other
        return 0


class FakeSinkPad:
    def __init__(self) -> None:
        self.linked = False

    def is_linked(self) -> bool:
        return self.linked


class FakeDepay:
    def __init__(self) -> None:
        self.sink = FakeSinkPad()

    def get_static_pad(self, name: str) -> FakeSinkPad | None:
        return self.sink if name == "sink" else None


class FakeGst:
    class PadLinkReturn:
        OK = 0


def test_rtsp_dynamic_pad_links_only_matching_video_codec() -> None:
    depay = FakeDepay()
    events: list[PipelineEvent] = []
    audio = FakePad("audio", "MPEG4-GENERIC")
    wrong_video = FakePad("video", "H265")
    expected_video = FakePad("video", "H264")

    for pad in (audio, wrong_video, expected_video):
        GStreamerPipelineFactory._on_rtsp_pad_added(
            object(), pad, FakeGst, depay, "video", "H264", events.append
        )

    assert audio.linked_to is None
    assert wrong_video.linked_to is None
    assert expected_video.linked_to is depay.sink
    assert events == []


@pytest.mark.parametrize("codec", ["", "auto", "jpeg", None])
def test_codec_cannot_be_guessed(tmp_path: Path, codec: object) -> None:
    with pytest.raises(ValueError, match="explicitly h264 or h265"):
        build_pipeline_spec(codec, tmp_path / "session-%05d.mkv")  # type: ignore[arg-type]


def test_recording_pattern_is_bounded_and_unambiguous(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        build_pipeline_spec("h264", "session-%05d.mkv")
    with pytest.raises(ValueError, match="one integer placeholder"):
        build_pipeline_spec("h264", tmp_path / "session.mkv")
    with pytest.raises(ValueError, match="unsupported placeholder"):
        build_pipeline_spec("h264", tmp_path / "%Y-session-%05d.mkv")
    with pytest.raises(ValueError, match="filename"):
        build_pipeline_spec("h264", tmp_path / "%05d" / "session.mkv")
    with pytest.raises(ValueError, match="exactly 600"):
        build_pipeline_spec(
            "h264", tmp_path / "session-%05d.mkv", segment_seconds=60
        )


def test_rtsp_secret_comes_from_environment_and_never_displays() -> None:
    uri = RtspUri.from_environment("CAMERA_URI", environ={"CAMERA_URI": SECRET_URI})

    assert SECRET_URI not in repr(uri)
    assert SECRET_URI not in str(uri)
    diagnostic = uri.redact(
        f"failed: {SECRET_URI}; password=camera-password", fallback="failed"
    )
    assert SECRET_URI not in diagnostic
    assert "camera-password" not in diagnostic
    assert "rtsp://" not in diagnostic


def test_short_rtsp_credentials_are_redacted_without_corrupting_uri_marker() -> None:
    secret = "rtsp://u:p@192.0.2.20/x"
    uri = RtspUri.from_memory(secret)

    diagnostic = uri.redact(f"failed {secret}; credential p", fallback="failed")

    assert secret not in diagnostic
    assert "credential p" not in diagnostic
    assert "[REDACTED_RTSP_URI]" in diagnostic


def test_missing_or_invalid_rtsp_secret_has_safe_error() -> None:
    with pytest.raises(Exception) as missing:
        RtspUri.from_environment("CAMERA_URI", environ={})
    with pytest.raises(Exception) as invalid:
        RtspUri.from_memory("not-an-rtsp-uri")

    assert SECRET_URI not in str(missing.value)
    assert "not-an-rtsp-uri" not in str(invalid.value)

    with pytest.raises(Exception) as unsafe_name:
        RtspUri.from_environment(SECRET_URI, environ={})
    assert SECRET_URI not in str(unsafe_name.value)


def test_rtsp_route_preflight_exposes_only_literal_ipv4() -> None:
    assert RtspUri.from_memory(SECRET_URI).destination_ipv4() == "192.0.2.20"
    with pytest.raises(Exception, match="literal IPv4"):
        RtspUri.from_memory("rtsp://user:secret@camera.local/live").destination_ipv4()


class FakeHandle:
    def __init__(self, *, play_result: bool = True) -> None:
        self.play_result = play_result
        self.played = False
        self.stops: list[tuple[bool, float]] = []

    def play(self) -> bool:
        self.played = True
        return self.play_result

    def stop(self, *, graceful: bool, timeout_seconds: float) -> bool:
        self.stops.append((graceful, timeout_seconds))
        return True


class FakeFactory:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.handle = FakeHandle()
        self.on_preview = None
        self.on_event = None
        self.uri_seen_in_memory: str | None = None

    def build(self, spec, rtsp_uri, on_preview_jpeg, on_event):
        del spec
        self.uri_seen_in_memory = rtsp_uri
        self.on_preview = on_preview_jpeg
        self.on_event = on_event
        if self.failure is not None:
            raise self.failure
        return self.handle

    def preview(self, jpeg: bytes) -> None:
        assert self.on_preview is not None
        self.on_preview(jpeg)

    def event(self, event: PipelineEvent) -> None:
        assert self.on_event is not None
        self.on_event(event)


def make_adapter(tmp_path: Path, factory, closed: list[ClosedFragment] | None = None):
    status = StatusStore()
    preview = PreviewStore()
    spec = build_pipeline_spec("h264", tmp_path / "session-%05d.mkv")
    adapter = CameraPipeline(
        status=status,
        preview=preview,
        rtsp_uri=RtspUri.from_memory(SECRET_URI),
        spec=spec,
        on_fragment_closed=(closed if closed is not None else []).append,
        factory=factory,
    )
    return adapter, status, preview


def test_fake_pipeline_becomes_ready_only_after_valid_jpeg(tmp_path: Path) -> None:
    factory = FakeFactory()
    adapter, status, preview = make_adapter(tmp_path, factory)

    assert adapter.start() is True
    assert factory.handle.played is True
    assert factory.uri_seen_in_memory == SECRET_URI
    assert status.snapshot()["components"]["camera"]["state"] == "starting"

    factory.preview(b"\xff\xd8frame\xff\xd9")

    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["state"] == "ready"
    assert snapshot["components"]["recording"]["state"] == "starting"
    assert preview.latest() is not None
    assert preview.latest().jpeg == b"\xff\xd8frame\xff\xd9"
    assert SECRET_URI not in repr(adapter)

    fragment = tmp_path / "session-00000.mkv"
    fragment.write_bytes(b"")
    factory.event(
        PipelineEvent(
            PipelineEventKind.FRAGMENT_OPENED,
            opened_fragment=OpenedFragment(fragment, 0),
        )
    )
    assert status.snapshot()["components"]["recording"]["state"] == "starting"
    fragment.write_bytes(b"matroska header")
    assert adapter.recording_progress_age_seconds is not None
    assert status.snapshot()["components"]["recording"]["state"] == "ready"


def test_fragment_closed_callback_receives_finalized_path(tmp_path: Path) -> None:
    factory = FakeFactory()
    closed: list[ClosedFragment] = []
    adapter, _status, _preview = make_adapter(tmp_path, factory, closed)
    assert adapter.start()
    fragment = ClosedFragment(tmp_path / "session-00000.mkv", 123, 456)

    factory.event(
        PipelineEvent(PipelineEventKind.FRAGMENT_CLOSED, fragment=fragment)
    )

    assert closed == [fragment]


def test_fragment_boundary_falls_back_to_open_close_duration(tmp_path: Path) -> None:
    events: list[PipelineEvent] = []
    tracker = _FragmentBoundaryTracker(events.append)
    location = str(tmp_path / "session-00000.mkv")

    tracker.receive(
        "splitmuxsink-fragment-opened",
        location=location,
        running_time_ns=1_000,
    )
    tracker.receive(
        "splitmuxsink-fragment-closed",
        location=location,
        running_time_ns=7_500,
    )

    assert events == [
        PipelineEvent(
            PipelineEventKind.FRAGMENT_OPENED,
            opened_fragment=OpenedFragment(Path(location), 1_000),
        ),
        PipelineEvent(
            PipelineEventKind.FRAGMENT_CLOSED,
            fragment=ClosedFragment(
                path=Path(location),
                running_time_ns=7_500,
                duration_ns=6_500,
            ),
        )
    ]


def test_fragment_boundary_uses_reported_duration_when_present(
    tmp_path: Path,
) -> None:
    events: list[PipelineEvent] = []
    tracker = _FragmentBoundaryTracker(events.append)
    location = str(tmp_path / "session-00000.mkv")
    tracker.receive(
        "splitmuxsink-fragment-opened", location=location, running_time_ns=100
    )
    tracker.receive(
        "splitmuxsink-fragment-closed",
        location=location,
        running_time_ns=900,
        duration_ns=750,
    )

    assert events[-1].fragment is not None
    assert events[-1].fragment.running_time_ns == 900
    assert events[-1].fragment.duration_ns == 750


@pytest.mark.parametrize(
    ("messages", "detail"),
    [
        (
            [("splitmuxsink-fragment-closed", "/tmp/segment-00000.mkv", 2)],
            "without a matching open",
        ),
        (
            [
                ("splitmuxsink-fragment-opened", "/tmp/segment-00000.mkv", 5),
                ("splitmuxsink-fragment-closed", "/tmp/segment-00000.mkv", 4),
            ],
            "out of order",
        ),
        (
            [("splitmuxsink-fragment-opened", None, 1)],
            "missing required fields",
        ),
        (
            [("splitmuxsink-fragment-opened", "/tmp/segment-00000.mkv", None)],
            "missing required fields",
        ),
    ],
)
def test_fragment_boundary_out_of_order_or_missing_fields_fail_safe(
    messages: list[tuple[str, str | None, int | None]], detail: str
) -> None:
    events: list[PipelineEvent] = []
    tracker = _FragmentBoundaryTracker(events.append)

    for message_name, location, running_time in messages:
        tracker.receive(
            message_name,
            location=location,
            running_time_ns=running_time,
        )

    assert events[-1].kind == PipelineEventKind.ERROR
    assert detail in events[-1].detail
    assert not any(event.fragment is not None for event in events)


def test_fragment_callback_rejects_path_outside_configured_pattern(
    tmp_path: Path,
) -> None:
    factory = FakeFactory()
    closed: list[ClosedFragment] = []
    adapter, status, _preview = make_adapter(tmp_path, factory, closed)
    assert adapter.start()

    factory.event(
        PipelineEvent(
            PipelineEventKind.FRAGMENT_CLOSED,
            fragment=ClosedFragment(tmp_path / "other-00000.mkv", 2, 1),
        )
    )

    assert closed == []
    assert status.snapshot()["components"]["recording"]["state"] == "error"


def test_recording_is_not_ready_until_regular_fragment_is_open(
    tmp_path: Path,
) -> None:
    factory = FakeFactory()
    adapter, status, _preview = make_adapter(tmp_path, factory)
    assert adapter.start()
    factory.preview(b"\xff\xd8frame\xff\xd9")
    missing = tmp_path / "session-00000.mkv"

    with pytest.raises(Exception, match="not confirmed"):
        factory.event(
            PipelineEvent(
                PipelineEventKind.FRAGMENT_OPENED,
                opened_fragment=OpenedFragment(missing, 0),
            )
        )

    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["state"] == "ready"
    assert snapshot["components"]["recording"]["state"] == "error"
    assert snapshot["tracking"]["armed"] is False


def removed_test_start_rejects_symlink_recording_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    factory = FakeFactory()
    adapter, status, _preview = make_adapter(linked, factory)

    assert adapter.start() is False
    assert factory.handle.played is False
    assert status.snapshot()["components"]["recording"]["state"] == "paused"


def test_start_refuses_to_overwrite_existing_fragment(tmp_path: Path) -> None:
    (tmp_path / "session-00000.mkv").write_bytes(b"keep")
    factory = FakeFactory()
    adapter, status, _preview = make_adapter(tmp_path, factory)

    assert adapter.start() is False
    assert (tmp_path / "session-00000.mkv").read_bytes() == b"keep"
    assert factory.handle.stops == [(False, 1.0)]
    assert status.snapshot()["components"]["camera"]["state"] == "error"


def test_start_refuses_dangling_fragment_symlink(tmp_path: Path) -> None:
    link = tmp_path / "session-00000.mkv"
    link.symlink_to(tmp_path / "outside-does-not-exist.mkv")
    factory = FakeFactory()
    adapter, status, _preview = make_adapter(tmp_path, factory)

    assert adapter.start() is False
    assert link.is_symlink()
    assert not (tmp_path / "outside-does-not-exist.mkv").exists()
    assert factory.handle.stops == [(False, 1.0)]
    assert status.snapshot()["components"]["camera"]["state"] == "error"


def test_failed_start_retains_unconfirmed_handle_and_blocks_late_readiness(
    tmp_path: Path,
) -> None:
    factory = FakeFactory()
    factory.handle.play_result = False

    def stuck_stop(*, graceful: bool, timeout_seconds: float) -> bool:
        factory.handle.stops.append((graceful, timeout_seconds))
        return False

    factory.handle.stop = stuck_stop  # type: ignore[method-assign]
    closed = []
    adapter, status, _preview = make_adapter(tmp_path, factory, closed)

    assert adapter.start() is False
    assert adapter.cleanup_unconfirmed is True
    assert adapter.terminal_kind is PipelineEventKind.ERROR
    late_path = tmp_path / "session-00000.mkv"
    late_path.write_bytes(b"late")
    factory.preview(b"\xff\xd8late preview\xff\xd9")
    factory.event(
        PipelineEvent(
            PipelineEventKind.FRAGMENT_OPENED,
            opened_fragment=OpenedFragment(late_path, 0),
        )
    )
    factory.event(
        PipelineEvent(
            PipelineEventKind.FRAGMENT_CLOSED,
            fragment=ClosedFragment(late_path, 1, 1),
        )
    )

    for component in ("inference", "servo"):
        status.update_component(component, "ready")
    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["state"] == "error"
    assert snapshot["components"]["recording"]["state"] == "paused"
    assert snapshot["arm_allowed"] is False
    assert closed == []


def test_bus_error_redacts_uri_and_forces_safe_state(tmp_path: Path) -> None:
    factory = FakeFactory()
    closed = []
    adapter, status, _preview = make_adapter(tmp_path, factory, closed)
    assert adapter.start()
    for component in ("inference", "servo"):
        status.update_component(component, "ready")
    factory.preview(b"\xff\xd8frame\xff\xd9")
    status.confirm_armed()

    factory.event(
        PipelineEvent(
            PipelineEventKind.ERROR,
            f"source failed at {SECRET_URI} using camera-password",
        )
    )

    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["state"] == "error"
    assert snapshot["components"]["recording"]["state"] == "paused"
    assert snapshot["tracking"]["armed"] is False
    assert SECRET_URI not in repr(snapshot)
    assert "camera-password" not in repr(snapshot)
    assert adapter.running is False
    assert adapter.terminal_kind is PipelineEventKind.ERROR

    # Callbacks already in flight after ERROR must not resurrect readiness or
    # create an archive completion marker from an untrusted boundary.
    factory.event(PipelineEvent(PipelineEventKind.EOS, "trailing EOS"))
    assert adapter.terminal_kind is PipelineEventKind.ERROR
    late_path = tmp_path / "session-00000.mkv"
    late_path.write_bytes(b"untrusted late fragment")
    factory.preview(b"\xff\xd8late frame\xff\xd9")
    factory.event(
        PipelineEvent(
            PipelineEventKind.FRAGMENT_OPENED,
            opened_fragment=OpenedFragment(late_path, 0),
        )
    )
    factory.event(
        PipelineEvent(
            PipelineEventKind.FRAGMENT_CLOSED,
            fragment=ClosedFragment(late_path, 1, 1),
        )
    )
    late_snapshot = status.snapshot()
    assert late_snapshot["components"]["camera"]["state"] == "error"
    assert late_snapshot["components"]["recording"]["state"] == "paused"
    assert late_snapshot["arm_allowed"] is False
    assert closed == []


def test_unexpected_eos_forces_safe_state(tmp_path: Path) -> None:
    factory = FakeFactory()
    adapter, status, _preview = make_adapter(tmp_path, factory)
    assert adapter.start()
    factory.preview(b"\xff\xd8frame\xff\xd9")

    factory.event(PipelineEvent(PipelineEventKind.EOS, "EOS"))

    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["state"] == "offline"
    assert snapshot["components"]["recording"]["state"] == "paused"
    assert snapshot["tracking"]["armed"] is False
    assert adapter.running is False
    assert adapter.terminal_kind is PipelineEventKind.EOS


def test_missing_gi_degrades_without_uri_leak(tmp_path: Path) -> None:
    def missing_bindings():
        raise GStreamerUnavailable("bindings unavailable")

    factory = GStreamerPipelineFactory(bindings_loader=missing_bindings)
    adapter, status, _preview = make_adapter(tmp_path, factory)

    assert adapter.start() is False
    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["state"] == "offline"
    assert snapshot["components"]["recording"]["state"] == "paused"
    assert SECRET_URI not in repr(snapshot)


def test_factory_exception_with_uri_is_sanitized(tmp_path: Path) -> None:
    factory = FakeFactory(failure=RuntimeError(f"cannot open {SECRET_URI}"))
    adapter, status, _preview = make_adapter(tmp_path, factory)

    assert adapter.start() is False
    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["state"] == "error"
    assert SECRET_URI not in repr(snapshot)
    assert "camera-password" not in repr(snapshot)
    assert adapter.cleanup_unconfirmed is True
    assert adapter.stop(timeout_seconds=0.0) is False
    assert adapter.start() is False


def test_play_exception_stops_handle_and_old_callbacks_cannot_reach_retry(
    tmp_path: Path,
) -> None:
    factory = FakeFactory()

    def fail_play() -> bool:
        raise RuntimeError("play failed")

    factory.handle.play = fail_play  # type: ignore[method-assign]
    adapter, status, preview = make_adapter(tmp_path, factory)

    assert adapter.start() is False
    assert factory.handle.stops == [(False, 1.0)]
    assert adapter.cleanup_unconfirmed is False
    old_preview = factory.on_preview
    old_event = factory.on_event
    assert old_preview is not None and old_event is not None

    factory.handle = FakeHandle()
    assert adapter.start() is True
    factory.preview(b"\xff\xd8new generation\xff\xd9")
    status.update_component("recording", "ready", "new generation")
    old_preview(b"\xff\xd8old generation\xff\xd9")
    old_event(PipelineEvent(PipelineEventKind.ERROR, "late old error"))

    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["state"] == "ready"
    assert snapshot["components"]["recording"]["detail"] == "new generation"
    assert preview.latest() is not None
    assert preview.latest().jpeg == b"\xff\xd8new generation\xff\xd9"


def test_play_exception_retains_handle_when_abort_is_unconfirmed(
    tmp_path: Path,
) -> None:
    factory = FakeFactory()

    def fail_play() -> bool:
        raise RuntimeError("play failed")

    def fail_stop(*, graceful: bool, timeout_seconds: float) -> bool:
        factory.handle.stops.append((graceful, timeout_seconds))
        return False

    factory.handle.play = fail_play  # type: ignore[method-assign]
    factory.handle.stop = fail_stop  # type: ignore[method-assign]
    adapter, status, preview = make_adapter(tmp_path, factory)

    assert adapter.start() is False
    assert adapter.cleanup_unconfirmed is True
    assert adapter.terminal_kind is PipelineEventKind.ERROR
    factory.preview(b"\xff\xd8late\xff\xd9")
    factory.event(PipelineEvent(PipelineEventKind.ERROR, "late error"))
    assert preview.latest() is None
    assert status.snapshot()["arm_allowed"] is False
    assert adapter.start() is False


def test_factory_exception_deactivates_captured_callbacks_and_blocks_reuse(
    tmp_path: Path,
) -> None:
    class PartialFactory:
        def __init__(self) -> None:
            self.preview_callback = None
            self.event_callback = None

        def build(self, _spec, _uri, on_preview_jpeg, on_event):
            self.preview_callback = on_preview_jpeg
            self.event_callback = on_event
            raise RuntimeError("factory failed after allocating native state")

    factory = PartialFactory()
    adapter, status, preview = make_adapter(tmp_path, factory)

    assert adapter.start() is False
    assert adapter.cleanup_unconfirmed is True
    assert factory.preview_callback is not None
    assert factory.event_callback is not None
    status.update_component("camera", "ready", "new owner")
    status.update_component("recording", "ready", "new owner")
    factory.preview_callback(b"\xff\xd8late\xff\xd9")
    factory.event_callback(PipelineEvent(PipelineEventKind.ERROR, "late"))

    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["detail"] == "new owner"
    assert snapshot["components"]["recording"]["detail"] == "new owner"
    assert preview.latest() is None
    assert adapter.stop(timeout_seconds=0.0) is False
    assert adapter.start() is False


def test_recording_progress_requires_byte_growth_not_only_mtime_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    factory = FakeFactory()
    adapter, _status, _preview = make_adapter(tmp_path, factory)
    now = [100.0]
    monkeypatch.setattr("shore_tracker.camera.pipeline.time.monotonic", lambda: now[0])
    assert adapter.start()
    fragment = tmp_path / "session-00000.mkv"
    fragment.write_bytes(b"a")
    factory.event(
        PipelineEvent(
            PipelineEventKind.FRAGMENT_OPENED,
            opened_fragment=OpenedFragment(fragment, 0),
        )
    )

    now[0] = 101.0
    fragment.touch()
    assert adapter.recording_progress_age_seconds == 1.0
    now[0] = 102.0
    fragment.write_bytes(b"ab")
    assert adapter.recording_progress_age_seconds == 0.0


def test_fragment_handoff_exposes_bounded_finalization_age(tmp_path: Path) -> None:
    factory = FakeFactory()
    started = threading.Event()
    release = threading.Event()

    def slow_handoff(_fragment: ClosedFragment) -> None:
        started.set()
        assert release.wait(2)

    status = StatusStore()
    adapter = CameraPipeline(
        status=status,
        preview=PreviewStore(),
        rtsp_uri=RtspUri.from_memory(SECRET_URI),
        spec=build_pipeline_spec("h264", tmp_path / "session-%05d.mkv"),
        on_fragment_closed=slow_handoff,
        factory=factory,
    )
    assert adapter.start()
    thread = threading.Thread(
        target=lambda: factory.event(
            PipelineEvent(
                PipelineEventKind.FRAGMENT_CLOSED,
                fragment=ClosedFragment(
                    tmp_path / "session-00000.mkv", 2, 1
                ),
            )
        )
    )
    thread.start()
    assert started.wait(1)
    assert adapter.finalization_age_seconds is not None
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert adapter.finalization_age_seconds is None


def test_unconfirmed_stop_is_bounded_while_accepted_fragment_handoff_finishes(
    tmp_path: Path,
) -> None:
    factory = FakeFactory()
    started = threading.Event()
    release = threading.Event()
    closed: list[ClosedFragment] = []

    def slow_handoff(fragment: ClosedFragment) -> None:
        started.set()
        assert release.wait(2)
        closed.append(fragment)

    def unconfirmed_stop(*, graceful: bool, timeout_seconds: float) -> bool:
        factory.handle.stops.append((graceful, timeout_seconds))
        return False

    factory.handle.stop = unconfirmed_stop  # type: ignore[method-assign]
    adapter = CameraPipeline(
        status=StatusStore(),
        preview=PreviewStore(),
        rtsp_uri=RtspUri.from_memory(SECRET_URI),
        spec=build_pipeline_spec("h264", tmp_path / "session-%05d.mkv"),
        on_fragment_closed=slow_handoff,
        factory=factory,
    )
    assert adapter.start()
    fragment = ClosedFragment(tmp_path / "session-00000.mkv", 2, 1)
    delivery = threading.Thread(
        target=lambda: factory.event(
            PipelineEvent(PipelineEventKind.FRAGMENT_CLOSED, fragment=fragment)
        )
    )
    delivery.start()
    assert started.wait(1)

    before = time.monotonic()
    assert adapter.stop(timeout_seconds=0.01) is False
    assert time.monotonic() - before < 0.2
    assert adapter.cleanup_unconfirmed is True
    release.set()
    delivery.join(2)

    assert not delivery.is_alive()
    assert closed == [fragment]
    assert adapter.terminal_kind is PipelineEventKind.ERROR


def test_stop_requests_eos_finalization(tmp_path: Path) -> None:
    factory = FakeFactory()
    adapter, status, _preview = make_adapter(tmp_path, factory)
    assert adapter.start()

    assert adapter.stop(timeout_seconds=0.25) is True

    assert factory.handle.stops == [(True, 0.25)]
    assert adapter.running is False
    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["state"] == "offline"
    assert snapshot["components"]["recording"]["state"] == "paused"


def test_successfully_stopped_pipeline_cannot_overwrite_new_session_status(
    tmp_path: Path,
) -> None:
    factory = FakeFactory()
    adapter, status, _preview = make_adapter(tmp_path, factory)
    assert adapter.start()
    assert adapter.stop(timeout_seconds=0.25)
    status.update_component("camera", "ready", "new generation")
    status.update_component("recording", "ready", "new generation")

    factory.preview(b"\xff\xd8late old preview\xff\xd9")
    factory.event(PipelineEvent(PipelineEventKind.ERROR, "late old error"))

    snapshot = status.snapshot()
    assert snapshot["components"]["camera"]["detail"] == "new generation"
    assert snapshot["components"]["recording"]["detail"] == "new generation"


class _WorkerFakeGst:
    class MessageType:
        ERROR = "error"
        EOS = "eos"
        ELEMENT = "element"

    class BusSyncReply:
        DROP = "drop"

    class State:
        PLAYING = "playing"
        NULL = "null"

    class StateChangeReturn:
        FAILURE = "failure"
        SUCCESS = "success"
        ASYNC = "async"

    class Event:
        @staticmethod
        def new_eos():
            return "eos-event"

    class FlowReturn:
        OK = "ok"
        EOS = "eos"
        ERROR = "error"


class _WorkerFakeBus:
    def __init__(self) -> None:
        self.handler = None

    def set_sync_handler(self, handler, _user_data) -> None:
        self.handler = handler


class _WorkerFakePipeline:
    def __init__(self) -> None:
        self.bus = _WorkerFakeBus()
        self.states: list[str] = []
        self.events: list[str] = []

    def get_bus(self):
        return self.bus

    def set_state(self, state):
        self.states.append(state)
        return "success"

    def send_event(self, event):
        self.events.append(event)
        return True


class _WorkerFakePreviewSink:
    def connect(self, *_args) -> None:
        return None


class _WorkerFakeStructure:
    def __init__(self, name: str, location: str, running_time: int) -> None:
        self.name = name
        self.location = location
        self.running_time = running_time

    def get_name(self) -> str:
        return self.name

    def get_string(self, name: str):
        return self.location if name == "location" else None

    def get_uint64(self, name: str):
        if name == "running-time":
            return True, self.running_time
        return False, 0


class _WorkerFakeMessage:
    def __init__(self, message_type: str, *, src=None, structure=None) -> None:
        self.type = message_type
        self.src = src
        self.structure = structure

    def get_structure(self):
        return self.structure


def _make_worker_handle(on_event, *, queue_size: int = 16):
    pipeline = _WorkerFakePipeline()
    segment_sink = object()
    handle = _GstPipelineHandle(
        gst=_WorkerFakeGst,
        pipeline=pipeline,
        preview_sink=_WorkerFakePreviewSink(),
        segment_sink=segment_sink,
        on_preview_jpeg=lambda _jpeg: None,
        on_event=on_event,
        diagnostic_redactor=RtspUri.from_memory(SECRET_URI),
        event_queue_size=queue_size,
    )
    return handle, pipeline, segment_sink


def _post_boundary(handle, segment_sink, name: str, path: Path, running: int):
    return handle._bus_message(
        None,
        _WorkerFakeMessage(
            _WorkerFakeGst.MessageType.ELEMENT,
            src=segment_sink,
            structure=_WorkerFakeStructure(name, str(path), running),
        ),
        None,
    )


def _post_eos(handle):
    return handle._bus_message(
        None,
        _WorkerFakeMessage(_WorkerFakeGst.MessageType.EOS),
        None,
    )


def test_gst_error_reports_source_and_redacts_debug_uri() -> None:
    events: list[PipelineEvent] = []
    handle, _pipeline, _sink = _make_worker_handle(events.append)

    class Source:
        @staticmethod
        def get_name() -> str:
            return "video_depay"

    class Error:
        message = "Internal data stream error"

    class ErrorMessage(_WorkerFakeMessage):
        def parse_error(self):
            return Error(), f"streaming stopped for {SECRET_URI}: not-linked"

    handle._bus_message(
        None,
        ErrorMessage(_WorkerFakeGst.MessageType.ERROR, src=Source()),
        None,
    )
    deadline = time.monotonic() + 1
    while not events and time.monotonic() < deadline:
        time.sleep(0.01)
    handle.stop(graceful=False, timeout_seconds=1)

    assert len(events) == 1
    assert events[0].kind is PipelineEventKind.ERROR
    assert "video_depay" in events[0].detail
    assert "not-linked" in events[0].detail
    assert SECRET_URI not in events[0].detail


def test_gst_sync_handler_never_runs_blocking_fragment_callback(
    tmp_path: Path,
) -> None:
    callback_started = threading.Event()
    release_callback = threading.Event()

    def blocking_callback(event: PipelineEvent) -> None:
        if event.kind == PipelineEventKind.FRAGMENT_CLOSED:
            callback_started.set()
            assert release_callback.wait(2)

    handle, _pipeline, sink = _make_worker_handle(blocking_callback)
    path = tmp_path / "session-00000.mkv"
    assert _post_boundary(
        handle, sink, "splitmuxsink-fragment-opened", path, 1
    ) == _WorkerFakeGst.BusSyncReply.DROP

    before = time.monotonic()
    assert _post_boundary(
        handle, sink, "splitmuxsink-fragment-closed", path, 2
    ) == _WorkerFakeGst.BusSyncReply.DROP
    elapsed = time.monotonic() - before

    assert elapsed < 0.05
    assert callback_started.wait(1)
    assert _post_eos(handle) == _WorkerFakeGst.BusSyncReply.DROP
    release_callback.set()
    assert handle.stop(graceful=True, timeout_seconds=2) is True


def test_gst_stop_waits_for_fragment_callback_and_deduplicates_close(
    tmp_path: Path,
) -> None:
    callback_started = threading.Event()
    release_callback = threading.Event()
    closed: list[ClosedFragment] = []

    def blocking_callback(event: PipelineEvent) -> None:
        if event.fragment is not None:
            closed.append(event.fragment)
            callback_started.set()
            assert release_callback.wait(2)

    handle, pipeline, sink = _make_worker_handle(blocking_callback)
    path = tmp_path / "session-00000.mkv"
    _post_boundary(handle, sink, "splitmuxsink-fragment-opened", path, 10)
    _post_boundary(handle, sink, "splitmuxsink-fragment-closed", path, 20)
    _post_boundary(handle, sink, "splitmuxsink-fragment-closed", path, 20)
    _post_eos(handle)
    assert callback_started.wait(1)

    result: list[bool] = []
    stopping = threading.Thread(
        target=lambda: result.append(
            handle.stop(graceful=True, timeout_seconds=2)
        )
    )
    stopping.start()
    time.sleep(0.05)
    assert stopping.is_alive()
    assert pipeline.states == []

    release_callback.set()
    stopping.join(2)
    assert result == [True]
    assert len(closed) == 1
    assert pipeline.states == [_WorkerFakeGst.State.NULL]


def test_gst_stop_does_not_resend_eos_after_upstream_eos_was_drained() -> None:
    events = []
    handle, pipeline, _sink = _make_worker_handle(events.append)
    _post_eos(handle)
    assert handle._drain_complete.wait(1)

    assert handle.stop(graceful=True, timeout_seconds=1) is True

    assert pipeline.events == []
    assert [event.kind for event in events] == [PipelineEventKind.EOS]


def test_gst_late_teardown_message_after_drain_is_not_fatal() -> None:
    events = []
    handle, _pipeline, _sink = _make_worker_handle(events.append)
    _post_eos(handle)
    assert handle._drain_complete.wait(1)

    assert handle.stop(graceful=True, timeout_seconds=1) is True
    assert _post_eos(handle) == _WorkerFakeGst.BusSyncReply.DROP
    assert handle._fatal_delivery_error is False


def test_gst_callback_failure_makes_graceful_stop_unconfirmed(
    tmp_path: Path,
) -> None:
    def failing_callback(event: PipelineEvent) -> None:
        if event.fragment is not None:
            raise RuntimeError("finalizer failed")

    handle, _pipeline, sink = _make_worker_handle(failing_callback)
    path = tmp_path / "session-00000.mkv"
    _post_boundary(handle, sink, "splitmuxsink-fragment-opened", path, 10)
    _post_boundary(handle, sink, "splitmuxsink-fragment-closed", path, 20)
    _post_eos(handle)

    assert handle.stop(graceful=True, timeout_seconds=2) is False


def test_preview_fps_does_not_change_encoded_recording_branch(tmp_path):
    path=tmp_path/'clip-%05d.mkv'
    slow=build_pipeline_spec('h265',path,preview_fps=5)
    fast=build_pipeline_spec('h265',path,preview_fps=20)
    assert properties(slow,'preview_caps')['caps']=='video/x-raw,framerate=5/1'
    assert properties(fast,'preview_caps')['caps']=='video/x-raw,framerate=20/1'
    for name in ('record_queue','record_parse','segment_sink'):
        assert properties(slow,name)==properties(fast,name)


def test_error_can_release_resources_without_claiming_finalization():
    handle, pipeline, _sink = _make_worker_handle(lambda event: None)
    handle._emit(PipelineEvent(PipelineEventKind.ERROR, 'camera disconnected'))
    _post_eos(handle)
    assert handle._drain_complete.wait(1)
    assert not handle.stop(graceful=True, timeout_seconds=1)
    assert handle.resources_released
    assert not handle.stop(graceful=True, timeout_seconds=0.1)
    assert pipeline.states == [_WorkerFakeGst.State.NULL]
    assert handle.resources_released


def test_failed_null_transition_is_not_resource_release_and_can_retry():
    handle, pipeline, _sink = _make_worker_handle(lambda event: None)
    pipeline.set_state = lambda state: _WorkerFakeGst.StateChangeReturn.FAILURE
    assert not handle.stop(graceful=False, timeout_seconds=1)
    assert not handle.resources_released
    pipeline.set_state = lambda state: _WorkerFakeGst.StateChangeReturn.SUCCESS
    assert handle.stop(graceful=False, timeout_seconds=1)
    assert handle.resources_released


def test_detach_failure_and_blocked_worker_do_not_release_resources():
    started, release = threading.Event(), threading.Event()
    def blocked(event):
        started.set()
        release.wait(2)
    handle, pipeline, _sink = _make_worker_handle(blocked)
    _post_eos(handle)
    assert started.wait(1)
    original_detach = pipeline.bus.set_sync_handler
    def fail_detach(*args):
        raise RuntimeError('detach failed')
    pipeline.bus.set_sync_handler = fail_detach
    try:
        assert not handle.stop(graceful=False, timeout_seconds=0.01)
        assert not handle.resources_released
        pipeline.bus.set_sync_handler = original_detach
        assert not handle.stop(graceful=False, timeout_seconds=0.01)
        assert not handle.resources_released
    finally:
        release.set()
        handle.stop(graceful=False, timeout_seconds=1)
    assert handle.resources_released


def test_partial_fragment_remains_unfinalized_after_release(tmp_path):
    events = []
    handle, _pipeline, sink = _make_worker_handle(events.append)
    partial = tmp_path / 'partial.mkv'
    partial.write_bytes(b'incomplete')
    _post_boundary(handle, sink, 'splitmuxsink-fragment-opened', partial, 1)
    handle._emit(PipelineEvent(PipelineEventKind.ERROR, 'disconnected'))
    assert not handle.stop(graceful=True, timeout_seconds=.05)
    assert not handle.stop(graceful=True, timeout_seconds=1)
    assert handle.resources_released
    assert partial.read_bytes() == b'incomplete'
    assert not any(event.fragment for event in events)


def test_camera_releases_failed_generation_and_ignores_late_callbacks(tmp_path):
    factory = FakeFactory()
    camera, status, _preview = make_adapter(tmp_path, factory)
    assert camera.start()
    camera._handle.stop = lambda **kwargs: False
    camera._handle.resources_released = True
    assert not camera.stop(timeout_seconds=.1)
    assert camera.resources_released
    assert not camera.cleanup_unconfirmed
    status.update_component('camera', 'ready', 'new generation')
    factory.event(PipelineEvent(PipelineEventKind.ERROR, 'late old error'))
    factory.preview(b'late old frame')
    assert status.snapshot()['components']['camera']['detail'] == 'new generation'
