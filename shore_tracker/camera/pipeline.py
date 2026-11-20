from __future__ import annotations

import importlib
import ipaddress
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit

from shore_tracker.preview import PreviewSession, PreviewStore
from shore_tracker.status import StatusStore


class CameraPipelineError(RuntimeError):
    """A camera pipeline error whose message is safe for local status output."""


class GStreamerUnavailable(CameraPipelineError):
    """Raised when PyGObject or the required GStreamer namespace is absent."""


class VideoCodec(str, Enum):
    H264 = "h264"
    H265 = "h265"


class RtspUri:
    """An in-memory RTSP credential with deliberately redacted display forms."""

    __slots__ = ("_secret", "_redaction_tokens")

    def __init__(self, _secret: str) -> None:
        self._secret = _secret
        self._redaction_tokens = self._find_redaction_tokens(_secret)

    @classmethod
    def from_environment(
        cls,
        variable_name: str,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> RtspUri:
        if not isinstance(variable_name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", variable_name
        ):
            raise CameraPipelineError("RTSP URI environment variable name is invalid")
        source = os.environ if environ is None else environ
        value = source.get(variable_name)
        if not value:
            raise CameraPipelineError(
                f"RTSP URI environment variable {variable_name} is not set"
            )
        return cls.from_memory(value)

    @classmethod
    def from_memory(cls, value: str) -> RtspUri:
        if not isinstance(value, str) or not value:
            raise CameraPipelineError("RTSP URI is missing")
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme.lower() in {"rtsp", "rtsps"} and bool(
                parsed.netloc and parsed.hostname
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise CameraPipelineError("RTSP URI is invalid")
        return cls(value)

    def __repr__(self) -> str:
        return "RtspUri([REDACTED])"

    def __str__(self) -> str:
        return "[REDACTED_RTSP_URI]"

    def _reveal(self) -> str:
        """Return the URI only for assigning the in-memory GStreamer property."""

        return self._secret

    def destination_ipv4(self) -> str:
        """Return only a literal IPv4 destination for a preflight route check."""

        try:
            hostname = urlsplit(self._secret).hostname
            return str(ipaddress.IPv4Address(hostname or ""))
        except (TypeError, ValueError):
            # Resolving a hostname could itself escape over the hotspot/DNS.
            raise CameraPipelineError(
                "RTSP destination must be a literal IPv4 address"
            ) from None

    def redact(self, value: object, *, fallback: str) -> str:
        text = str(value) if value is not None else ""
        sentinel = "\x00__SHORE_RTSP_REDACTED__\x00"
        if self._secret:
            text = text.replace(self._secret, sentinel)
        text = re.sub(
            r"(?i)\brtsps?://[^\s<>\"']+",
            sentinel,
            text,
        )
        parts = text.split(sentinel)
        for index, part in enumerate(parts):
            for token in self._redaction_tokens:
                part = part.replace(token, "[REDACTED]")
            parts[index] = part
        text = "[REDACTED_RTSP_URI]".join(parts)
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return (text or fallback)[:240]

    @staticmethod
    def _find_redaction_tokens(value: str) -> tuple[str, ...]:
        try:
            parsed = urlsplit(value)
            query_values = tuple(
                item
                for pair in parse_qsl(parsed.query, keep_blank_values=True)
                for item in pair
                if item
            )
            candidates = (
                value,
                unquote(value),
                parsed.netloc,
                unquote(parsed.netloc),
                parsed.path,
                unquote(parsed.path),
                parsed.query,
                unquote(parsed.query),
                parsed.fragment,
                unquote(parsed.fragment),
                parsed.username or "",
                parsed.password or "",
                unquote(parsed.username or ""),
                unquote(parsed.password or ""),
                *query_values,
            )
        except (TypeError, ValueError):
            candidates = ()
        # Very short tokens are usually punctuation or common words.  The full
        # URI pattern above still removes them when they occur in a URI.
        return tuple(
            sorted(
                {token for token in candidates if token},
                key=len,
                reverse=True,
            )
        )


@dataclass(frozen=True)
class ElementSpec:
    factory: str
    name: str
    properties: tuple[tuple[str, object], ...] = ()

    def property(self, name: str) -> object:
        for candidate, value in self.properties:
            if candidate == name:
                return value
        raise KeyError(name)


@dataclass(frozen=True)
class LinkSpec:
    source: str
    target: str
    source_pad: str | None = None
    target_pad: str | None = None
    request_source: bool = False
    request_target: bool = False


@dataclass(frozen=True)
class PipelineSpec:
    codec: VideoCodec
    elements: tuple[ElementSpec, ...]
    links: tuple[LinkSpec, ...]
    recording_pattern: Path | None
    segment_seconds: int
    expected_rtp_media: str = "video"
    expected_rtp_encoding: str = "H264"
    rtsp_transport: str = "tcp"
    rtsp_source_name: str = "rtsp_source"
    depay_name: str = "video_depay"
    segment_sink_name: str = "segment_sink"
    preview_sink_name: str = "preview_sink"
    audio_enabled: bool = False

    def element(self, name: str) -> ElementSpec:
        for element in self.elements:
            if element.name == name:
                return element
        raise KeyError(name)


_CODEC_ELEMENTS = {
    VideoCodec.H264: ("H264", "rtph264depay", "h264parse"),
    VideoCodec.H265: ("H265", "rtph265depay", "h265parse"),
}


def build_pipeline_spec(
    codec: VideoCodec | str,
    recording_pattern: str | Path | None,
    *,
    segment_seconds: int = 600,
    rtsp_latency_ms: int = 200,
    preview_width: int = 960,
    preview_height: int = 540,
    preview_fps: int = 20,
    rotation_degrees: int = 0,
    native_display: bool = False,
    decoded_inference: bool = False,
    inference_frame_fps: int = 4,
) -> PipelineSpec:
    """Build a URI-free, inspectable specification for one encoded video stream."""

    try:
        selected = codec if isinstance(codec, VideoCodec) else VideoCodec(codec.lower())
    except (AttributeError, ValueError):
        raise ValueError("codec must be explicitly h264 or h265") from None
    if segment_seconds != 600:
        raise ValueError("camera recording segments must be exactly 600 seconds")
    if not 0 <= rtsp_latency_ms <= 5_000:
        raise ValueError("rtsp_latency_ms must be between 0 and 5000")
    if min(preview_width, preview_height, preview_fps) <= 0:
        raise ValueError("preview dimensions and frame rate must be positive")
    if type(rotation_degrees) is not int or rotation_degrees not in {0, 180}:
        raise ValueError("rotation_degrees must be 0 or 180")

    pattern = Path(recording_pattern) if recording_pattern is not None else None
    if pattern is not None:
        _validate_recording_pattern(pattern)
    encoding, depay, parser = _CODEC_ELEMENTS[selected]

    elements = (
        ElementSpec(
            "rtspsrc",
            "rtsp_source",
            (
                ("protocols", "tcp"),
                ("latency", rtsp_latency_ms),
                ("drop-on-latency", True),
                ("debug", False),
            ),
        ),
        ElementSpec(depay, "video_depay"),
        ElementSpec(
            "queue",
            "preview_queue",
            (
                ("leaky", "downstream"),
                ("max-size-buffers", 2),
                ("max-size-bytes", 0),
                ("max-size-time", 0),
            ),
        ),
        ElementSpec(parser, "preview_parse", (("config-interval", -1),)),
        # This project targets Jetson. The audited JetPack image provides the
        # NVIDIA decoder/converter and does not provide libav's avdec elements.
        ElementSpec("nvv4l2decoder", "preview_decoder"),
        # Preview publishes independently. Inference samples the latest upright
        # JPEG at its own pace; it never blocks the preview producer.
        ElementSpec(
            "nvvidconv", "preview_nvconvert",
            (("flip-method", 2 if rotation_degrees == 180 else 0),),
        ),
        ElementSpec(
            "capsfilter",
            "preview_raw_caps",
            (("caps", f"video/x-raw,width={preview_width},height={preview_height}"),),
        ),
        ElementSpec("videoconvert", "preview_convert"),
        ElementSpec("videorate", "preview_rate", (("drop-only", True),)),
        ElementSpec(
            "capsfilter",
            "preview_caps",
            (
                (
                    "caps",
                    f"video/x-raw,framerate={preview_fps}/1",
                ),
            ),
        ),
        ElementSpec("jpegenc", "preview_jpeg", (("quality", 80),)),
        ElementSpec(
            "appsink",
            "preview_sink",
            (
                ("emit-signals", True),
                ("max-buffers", 1),
                ("drop", True),
                ("sync", False),
                ("wait-on-eos", False),
            ),
        ),
    )
    if pattern is None:
        links = (LinkSpec("video_depay", "preview_queue"),)
    else:
        elements += (
            ElementSpec("tee", "encoded_tee", (("allow-not-linked", True),)),
            ElementSpec("queue", "record_queue"),
            ElementSpec(parser, "record_parse", (("config-interval", -1),)),
            ElementSpec("splitmuxsink", "segment_sink", (
                ("async-finalize", True),
                ("muxer-factory", "matroskamux"),
                ("max-size-time", segment_seconds * 1_000_000_000),
                ("max-size-bytes", 0),
                ("max-files", 0),
                ("send-keyframe-requests", True),
            )),
        )
        links = (
            LinkSpec("video_depay", "encoded_tee"),
            LinkSpec("encoded_tee", "record_queue", "src_%u", "sink", request_source=True),
            LinkSpec("record_queue", "record_parse"),
            LinkSpec("record_parse", "segment_sink", "src", "video", request_target=True),
            LinkSpec("encoded_tee", "preview_queue", "src_%u", "sink", request_source=True),
        )
    links += (
        LinkSpec("preview_queue", "preview_parse"),
        LinkSpec("preview_parse", "preview_decoder"),
        LinkSpec("preview_decoder", "preview_nvconvert"),
        LinkSpec("preview_nvconvert", "preview_raw_caps"),
        LinkSpec("preview_raw_caps", "preview_convert"),
        LinkSpec("preview_convert", "preview_rate"),
        LinkSpec("preview_rate", "preview_caps"),
        LinkSpec("preview_caps", "preview_jpeg"),
        LinkSpec("preview_jpeg", "preview_sink"),
    )
    if native_display:
        elements += (
            ElementSpec("tee", "native_tee", (("allow-not-linked", True),)),
            ElementSpec("queue", "native_preview_queue", (("leaky", "downstream"), ("max-size-buffers", 2), ("max-size-bytes", 0), ("max-size-time", 0))),
            ElementSpec("queue", "native_queue", (("leaky", "downstream"), ("max-size-buffers", 1), ("max-size-bytes", 0), ("max-size-time", 0))),
            ElementSpec("nvvidconv", "native_convert", (("flip-method", 2 if rotation_degrees == 180 else 0),)),
            ElementSpec("capsfilter", "native_caps", (("caps", f"video/x-raw,format=BGRx,width={preview_width},height={preview_height}"),)),
            ElementSpec("videorate", "native_rate", (("drop-only", True),)),
            ElementSpec("capsfilter", "native_rate_caps", (("caps", f"video/x-raw,format=BGRx,framerate={preview_fps}/1"),)),
            ElementSpec("appsink", "native_sink", (("emit-signals", True), ("max-buffers", 1), ("drop", True), ("sync", False), ("wait-on-eos", False))),
        )
        # Branch off the existing decoder, preserving inference and recording.
        links = tuple(link for link in links if link.source != "preview_decoder") + (
            LinkSpec("preview_decoder", "native_tee"),
            LinkSpec("native_tee", "native_preview_queue", "src_%u", "sink", request_source=True),
            LinkSpec("native_preview_queue", "preview_nvconvert"),
            LinkSpec("native_tee", "native_queue", "src_%u", "sink", request_source=True),
            LinkSpec("native_queue", "native_convert"),
            LinkSpec("native_convert", "native_caps"),
            LinkSpec("native_caps", "native_rate"),
            LinkSpec("native_rate", "native_rate_caps"),
            LinkSpec("native_rate_caps", "native_sink"),
        )
    if decoded_inference:
        if type(inference_frame_fps) is not int or not 1 <= inference_frame_fps <= 20:
            raise ValueError("inference_frame_fps must be in [1, 20]")
        elements += (
            ElementSpec("tee", "decoded_tee", (("allow-not-linked", True),)),
            ElementSpec("queue", "display_queue", (("leaky", "downstream"), ("max-size-buffers", 2), ("max-size-bytes", 0), ("max-size-time", 0))),
            ElementSpec("videorate", "display_rate", (("drop-only", True),)),
            ElementSpec("capsfilter", "display_rate_caps", (("caps", f"video/x-raw(memory:NVMM),framerate={preview_fps}/1"),)),
            ElementSpec("queue", "inference_queue", (("leaky", "downstream"), ("max-size-buffers", 2), ("max-size-bytes", 0), ("max-size-time", 0))),
            ElementSpec("videorate", "inference_rate", (("drop-only", True),)),
            ElementSpec("capsfilter", "inference_rate_caps", (("caps", f"video/x-raw(memory:NVMM),framerate={inference_frame_fps}/1"),)),
            ElementSpec("nvvidconv", "inference_convert", (("flip-method", 2 if rotation_degrees == 180 else 0),)),
            ElementSpec("capsfilter", "inference_raw_caps", (("caps", "video/x-raw,format=RGBA"),)),
            ElementSpec("appsink", "inference_sink", (("emit-signals", True), ("max-buffers", 1), ("drop", True), ("sync", False), ("wait-on-eos", False))),
        )
        links = tuple(link for link in links if link.source != "preview_decoder") + (
            LinkSpec("preview_decoder", "decoded_tee"),
            LinkSpec("decoded_tee", "display_queue", "src_%u", "sink", request_source=True),
            LinkSpec("display_queue", "display_rate"),
            LinkSpec("display_rate", "display_rate_caps"),
            LinkSpec("display_rate_caps", "native_tee" if native_display else "preview_nvconvert"),
            LinkSpec("decoded_tee", "inference_queue", "src_%u", "sink", request_source=True),
            LinkSpec("inference_queue", "inference_rate"),
            LinkSpec("inference_rate", "inference_rate_caps"),
            LinkSpec("inference_rate_caps", "inference_convert"),
            LinkSpec("inference_convert", "inference_raw_caps"),
            LinkSpec("inference_raw_caps", "inference_sink"),
        )
    return PipelineSpec(
        codec=selected,
        elements=elements,
        links=links,
        recording_pattern=pattern,
        segment_seconds=segment_seconds,
        expected_rtp_encoding=encoding,
    )


def _validate_recording_pattern(pattern: Path) -> None:
    text = str(pattern)
    if not pattern.is_absolute():
        raise ValueError("recording_pattern must be an absolute path")
    if pattern.suffix.lower() != ".mkv":
        raise ValueError("recording_pattern must use the .mkv extension")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError("recording_pattern contains control characters")
    if "%" in str(pattern.parent):
        raise ValueError("recording_pattern placeholder must be in the filename")
    matches = list(re.finditer(r"(?<!%)%(?:0[1-9][0-9]*)?d", text))
    if len(matches) != 1:
        raise ValueError("recording_pattern must contain one integer placeholder")
    match = matches[0]
    if "%" in (text[: match.start()] + text[match.end() :]):
        raise ValueError("recording_pattern contains an unsupported placeholder")


def _recording_path_matches(pattern: Path, candidate: Path) -> bool:
    if not candidate.is_absolute() or candidate.parent != pattern.parent:
        return False
    match = re.search(r"%(?:0([1-9][0-9]*))?d", pattern.name)
    if match is None:
        return False
    width = int(match.group(1)) if match.group(1) else 1
    filename_expression = (
        re.escape(pattern.name[: match.start()])
        + rf"[0-9]{{{width},}}"
        + re.escape(pattern.name[match.end() :])
    )
    return re.fullmatch(filename_expression, candidate.name) is not None


def _recording_pattern_has_existing_fragment(pattern: Path) -> bool:
    if not pattern.parent.exists():
        return False
    return any(
        _recording_path_matches(pattern, path)
        for path in pattern.parent.iterdir()
    )


class PipelineEventKind(str, Enum):
    ERROR = "error"
    EOS = "eos"
    FRAGMENT_OPENED = "fragment_opened"
    FRAGMENT_CLOSED = "fragment_closed"


@dataclass(frozen=True)
class ClosedFragment:
    path: Path
    running_time_ns: int | None = None
    duration_ns: int | None = None

    def __post_init__(self) -> None:
        if self.running_time_ns is not None and self.running_time_ns < 0:
            raise ValueError("fragment running time cannot be negative")
        if self.duration_ns is not None and self.duration_ns <= 0:
            raise ValueError("fragment duration must be positive")


@dataclass(frozen=True)
class OpenedFragment:
    path: Path
    running_time_ns: int

    def __post_init__(self) -> None:
        if self.running_time_ns < 0:
            raise ValueError("fragment running time cannot be negative")


@dataclass(frozen=True)
class PipelineEvent:
    kind: PipelineEventKind
    detail: str = ""
    fragment: ClosedFragment | None = None
    opened_fragment: OpenedFragment | None = None


class _FragmentBoundaryTracker:
    """Match splitmux open/close boundaries without relying on Gst 1.26."""

    def __init__(self, on_event: Callable[[PipelineEvent], None]) -> None:
        self._on_event = on_event
        self._opened_at: dict[Path, int] = {}
        self._closed: set[Path] = set()
        self._lock = threading.Lock()

    @property
    def has_open_fragments(self) -> bool:
        with self._lock:
            return bool(self._opened_at)

    def receive(
        self,
        message_name: str,
        *,
        location: str | None,
        running_time_ns: int | None,
        duration_ns: int | None = None,
    ) -> None:
        if message_name not in {
            "splitmuxsink-fragment-opened",
            "splitmuxsink-fragment-closed",
        }:
            return
        if not location or running_time_ns is None or running_time_ns < 0:
            self._error("recording fragment boundary is missing required fields")
            return
        path = Path(location)
        opened_event: PipelineEvent | None = None
        with self._lock:
            if message_name == "splitmuxsink-fragment-opened":
                duplicate = path in self._opened_at or path in self._closed
                if duplicate:
                    opened_at = None
                else:
                    self._opened_at[path] = running_time_ns
                    opened_event = PipelineEvent(
                        PipelineEventKind.FRAGMENT_OPENED,
                        opened_fragment=OpenedFragment(path, running_time_ns),
                    )
            else:
                if path in self._closed:
                    # splitmuxsink can repeat an element message while tearing
                    # down. A closed pathname is immutable for one handle, so
                    # never deliver it to the application twice.
                    return
                opened_at = self._opened_at.pop(path, None)
                if opened_at is not None:
                    self._closed.add(path)

        if message_name == "splitmuxsink-fragment-opened":
            if opened_event is not None:
                self._on_event(opened_event)
                return
            self._error("recording fragment opened more than once")
            return
        if opened_at is None:
            self._error("recording fragment closed without a matching open")
            return
        if running_time_ns <= opened_at:
            self._error("recording fragment boundaries are out of order")
            return
        calculated_duration = running_time_ns - opened_at
        if duration_ns is None:
            duration_ns = calculated_duration
        if duration_ns <= 0:
            self._error("recording fragment duration is invalid")
            return
        self._on_event(
            PipelineEvent(
                PipelineEventKind.FRAGMENT_CLOSED,
                fragment=ClosedFragment(
                    path=path,
                    running_time_ns=running_time_ns,
                    duration_ns=duration_ns,
                ),
            )
        )

    def _error(self, detail: str) -> None:
        self._on_event(PipelineEvent(PipelineEventKind.ERROR, detail))


class PipelineHandle(Protocol):
    def play(self) -> bool: ...

    def stop(self, *, graceful: bool, timeout_seconds: float) -> bool: ...


class PipelineFactory(Protocol):
    def build(
        self,
        spec: PipelineSpec,
        rtsp_uri: str,
        on_preview_jpeg: Callable[..., None],
        on_event: Callable[[PipelineEvent], None],
    ) -> PipelineHandle: ...


@dataclass(frozen=True)
class _GstBindings:
    Gst: Any
    GstRtsp: Any


def _load_gstreamer() -> _GstBindings:
    try:
        gi = importlib.import_module("gi")
        gi.require_version("Gst", "1.0")
        gi.require_version("GstRtsp", "1.0")
        gst = importlib.import_module("gi.repository.Gst")
        gst_rtsp = importlib.import_module("gi.repository.GstRtsp")
        gst.init(None)
    except (ImportError, AttributeError, ValueError, RuntimeError):
        raise GStreamerUnavailable("GStreamer Python bindings are unavailable") from None
    return _GstBindings(Gst=gst, GstRtsp=gst_rtsp)


class GStreamerPipelineFactory:
    """Real factory; constructing it does not import GI or access the camera."""

    def __init__(
        self,
        *,
        bindings_loader: Callable[[], _GstBindings] = _load_gstreamer,
        on_decoded_frame: Callable[[bytes, int, int, int], None] | None = None,
        on_native_frame: Callable | None = None,
    ) -> None:
        self._bindings_loader = bindings_loader
        self._on_decoded_frame = on_decoded_frame
        self._on_native_frame = on_native_frame

    def build(
        self,
        spec: PipelineSpec,
        rtsp_uri: str,
        on_preview_jpeg: Callable[[bytes], None],
        on_event: Callable[[PipelineEvent], None],
    ) -> PipelineHandle:
        redactor = RtspUri.from_memory(rtsp_uri)
        try:
            return self._build(
                spec,
                redactor,
                on_preview_jpeg,
                on_event,
            )
        except GStreamerUnavailable:
            raise
        except BaseException as exc:
            raise CameraPipelineError(
                redactor.redact(exc, fallback="GStreamer pipeline could not be built")
            ) from None

    def _build(
        self,
        spec: PipelineSpec,
        rtsp_uri: RtspUri,
        on_preview_jpeg: Callable[[bytes], None],
        on_event: Callable[[PipelineEvent], None],
    ) -> PipelineHandle:
        bindings = self._bindings_loader()
        gst = bindings.Gst
        pipeline = gst.Pipeline.new("shore_camera")
        if pipeline is None:
            raise CameraPipelineError("GStreamer pipeline could not be created")

        elements: dict[str, Any] = {}
        for definition in spec.elements:
            element = gst.ElementFactory.make(definition.factory, definition.name)
            if element is None:
                raise CameraPipelineError(
                    f"required GStreamer element is unavailable: {definition.factory}"
                )
            for property_name, value in definition.properties:
                # JetPack 4 ships Gst 1.14: splitmuxsink finalizes synchronously
                # and accepts a muxer object rather than a muxer factory name.
                if definition.factory == "splitmuxsink":
                    if property_name == "async-finalize" and element.find_property(property_name) is None:
                        continue
                    if property_name == "muxer-factory" and element.find_property(property_name) is None:
                        muxer = gst.ElementFactory.make(str(value), None)
                        if muxer is None:
                            raise CameraPipelineError("Matroska muxer is unavailable")
                        element.set_property("muxer", muxer)
                        continue
                element.set_property(
                    property_name,
                    self._property_value(
                        bindings, definition.factory, property_name, value
                    ),
                )
            pipeline.add(element)
            elements[definition.name] = element

        source = elements[spec.rtsp_source_name]
        # This is the sole point where the secret URI enters GStreamer.  It was
        # intentionally absent while parsing/building the public specification.
        source.set_property("location", rtsp_uri._reveal())
        segment_sink = elements.get(spec.segment_sink_name)
        if spec.recording_pattern is not None:
            segment_sink.set_property("location", str(spec.recording_pattern))

        for link in spec.links:
            self._link(gst, elements, link)

        source.connect(
            "pad-added",
            self._on_rtsp_pad_added,
            gst,
            elements[spec.depay_name],
            spec.expected_rtp_media,
            spec.expected_rtp_encoding,
            on_event,
        )
        return _GstPipelineHandle(
            gst=gst,
            pipeline=pipeline,
            preview_sink=elements[spec.preview_sink_name],
            segment_sink=segment_sink,
            on_preview_jpeg=on_preview_jpeg,
            on_event=on_event,
            diagnostic_redactor=rtsp_uri,
            inference_sink=elements.get("inference_sink"),
            on_decoded_frame=self._on_decoded_frame,
            native_sink=elements.get("native_sink"),
            on_native_frame=self._on_native_frame,
        )

    @staticmethod
    def _property_value(
        bindings: _GstBindings,
        factory: str,
        name: str,
        value: object,
    ) -> object:
        if factory == "rtspsrc" and name == "protocols" and value == "tcp":
            return bindings.GstRtsp.RTSPLowerTrans.TCP
        if factory == "queue" and name == "leaky" and value == "downstream":
            queue_leaky = getattr(bindings.Gst, "QueueLeaky", None)
            return queue_leaky.DOWNSTREAM if queue_leaky is not None else 2
        if factory == "capsfilter" and name == "caps":
            return bindings.Gst.Caps.from_string(str(value))
        return value

    @classmethod
    def _link(
        cls,
        gst: Any,
        elements: Mapping[str, Any],
        link: LinkSpec,
    ) -> None:
        source = elements[link.source]
        target = elements[link.target]
        if not link.request_source and not link.request_target:
            if not source.link(target):
                raise CameraPipelineError(
                    f"GStreamer elements could not link: {link.source} to {link.target}"
                )
            return

        source_pad = (
            cls._request_pad(source, link.source_pad)
            if link.request_source
            else source.get_static_pad(link.source_pad)
        )
        target_pad = (
            cls._request_pad(target, link.target_pad)
            if link.request_target
            else target.get_static_pad(link.target_pad)
        )
        if source_pad is None or target_pad is None:
            raise CameraPipelineError(
                f"GStreamer pad is unavailable: {link.source} to {link.target}"
            )
        if source_pad.link(target_pad) != gst.PadLinkReturn.OK:
            raise CameraPipelineError(
                f"GStreamer pads could not link: {link.source} to {link.target}"
            )

    @staticmethod
    def _request_pad(element: Any, template: str | None) -> Any:
        if not template:
            return None
        request_pad_simple = getattr(element, "request_pad_simple", None)
        if request_pad_simple is not None:
            return request_pad_simple(template)
        return element.get_request_pad(template)

    @staticmethod
    def _on_rtsp_pad_added(
        _source: Any,
        pad: Any,
        gst: Any,
        depay: Any,
        expected_media: str,
        expected_encoding: str,
        on_event: Callable[[PipelineEvent], None],
    ) -> None:
        try:
            caps = pad.get_current_caps() or pad.query_caps(None)
            if caps is None or caps.get_size() < 1:
                return
            structure = caps.get_structure(0)
            media = structure.get_string("media") or ""
            encoding = structure.get_string("encoding-name") or ""
            if media.lower() != expected_media or encoding.upper() != expected_encoding:
                return
            sink_pad = depay.get_static_pad("sink")
            if sink_pad is None or sink_pad.is_linked():
                return
            if pad.link(sink_pad) != gst.PadLinkReturn.OK:
                on_event(
                    PipelineEvent(
                        PipelineEventKind.ERROR,
                        "configured RTSP video pad could not be linked",
                    )
                )
        except BaseException:
            on_event(
                PipelineEvent(
                    PipelineEventKind.ERROR,
                    "RTSP video pad negotiation failed",
                )
            )


class _GstPipelineHandle:
    def __init__(
        self,
        *,
        gst: Any,
        pipeline: Any,
        preview_sink: Any,
        segment_sink: Any,
        on_preview_jpeg: Callable[[bytes], None],
        on_event: Callable[[PipelineEvent], None],
        diagnostic_redactor: RtspUri,
        event_queue_size: int = 1024,
        native_sink: Any = None,
        on_native_frame: Callable | None = None,
        inference_sink: Any = None,
        on_decoded_frame: Callable[[bytes, int, int, int], None] | None = None,
    ) -> None:
        if event_queue_size < 4:
            raise ValueError("GStreamer event queue must hold at least four messages")
        self._gst = gst
        self._pipeline = pipeline
        self._segment_sink = segment_sink
        self._on_preview_jpeg = on_preview_jpeg
        self._on_event = on_event
        self._diagnostic_redactor = diagnostic_redactor
        self._bus_events: queue.Queue[_QueuedBusEvent] = queue.Queue(
            maxsize=event_queue_size
        )
        self._bus_state_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._pending_bus_events = 0
        self._eos_received = False
        self._drain_complete = threading.Event()
        self._fatal_delivery_error = False
        self._overflow_notified = False
        self._accept_bus_events = True
        self._shutdown_worker = threading.Event()
        self._null_state_requested = False
        self._null_state_confirmed = False
        self._bus_detached = False
        self._eos_requested = False
        self._fragment_boundaries = _FragmentBoundaryTracker(self._emit)
        preview_sink.connect("new-sample", self._new_sample)
        self._on_decoded_frame = on_decoded_frame
        self._on_native_frame = on_native_frame
        if native_sink is not None:
            if on_native_frame is None:
                raise CameraPipelineError("native display requires a frame consumer")
            native_sink.connect("new-sample", self._new_native_sample)
        if inference_sink is not None:
            if on_decoded_frame is None:
                raise CameraPipelineError("decoded inference requires a frame consumer")
            gi = importlib.import_module("gi")
            gi.require_version("GstVideo", "1.0")
            self._gst_video = importlib.import_module("gi.repository.GstVideo")
            inference_sink.connect("new-sample", self._new_decoded_sample)
        self._bus = pipeline.get_bus()
        self._bus.set_sync_handler(self._bus_message, None)
        self._bus_worker = threading.Thread(
            target=self._run_bus_worker,
            name="shore-gstreamer-events",
            daemon=True,
        )
        self._bus_worker.start()

    def play(self) -> bool:
        result = self._pipeline.set_state(self._gst.State.PLAYING)
        return result != self._gst.StateChangeReturn.FAILURE

    @property
    def resources_released(self) -> bool:
        """Teardown confirmation is independent of recording finalization."""
        with self._bus_state_lock:
            return (
                self._null_state_confirmed and self._bus_detached
                and self._pending_bus_events == 0
                and not self._bus_worker.is_alive()
            )

    def stop(self, *, graceful: bool, timeout_seconds: float) -> bool:
        timeout_seconds = max(0.0, timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        with self._stop_lock:
            with self._bus_state_lock:
                eos_already_received = self._eos_received
            eos_sent = True
            if (
                graceful
                and not eos_already_received
                and not self._eos_requested
                and not self._null_state_requested
            ):
                self._eos_requested = True
                eos_sent = bool(self._pipeline.send_event(self._gst.Event.new_eos()))
                if not eos_sent:
                    with self._bus_state_lock:
                        self._fatal_delivery_error = True
            elif graceful and eos_already_received:
                # An unexpected but clean upstream EOS is already the terminal
                # boundary.  Sending a second EOS to an ended pipeline may be
                # rejected even though every fragment callback was drained.
                self._eos_requested = True

            drained = True
            if graceful and eos_sent and not self._null_state_requested:
                drained = self._drain_complete.wait(
                    max(0.0, deadline - time.monotonic())
                )
            elif graceful:
                drained = self._drain_complete.is_set()

            if not self._null_state_confirmed:
                self._null_state_requested = True
                with self._bus_state_lock:
                    self._accept_bus_events = False
                result = self._pipeline.set_state(self._gst.State.NULL)
                self._null_state_confirmed = result == self._gst.StateChangeReturn.SUCCESS
                if result == self._gst.StateChangeReturn.ASYNC:
                    result, state, _pending = self._pipeline.get_state(
                        int(max(0.0, deadline - time.monotonic()) * 1_000_000_000)
                    )
                    self._null_state_confirmed = (
                        result == self._gst.StateChangeReturn.SUCCESS
                        and state == self._gst.State.NULL
                    )
            if not self._bus_detached:
                try:
                    self._bus.set_sync_handler(None, None)
                    self._bus_detached = True
                except BaseException:
                    with self._bus_state_lock:
                        self._fatal_delivery_error = True
            self._shutdown_worker.set()

            self._bus_worker.join(max(0.0, deadline - time.monotonic()))
            with self._bus_state_lock:
                delivery_ok = not self._fatal_delivery_error
            worker_stopped = not self._bus_worker.is_alive()
            return bool(eos_sent and drained and delivery_ok and worker_stopped
                        and self.resources_released)

    def _new_sample(self, sink: Any) -> Any:
        try:
            sample = sink.emit("pull-sample")
            if sample is None:
                return self._gst.FlowReturn.EOS
            buffer = sample.get_buffer()
            if buffer is None:
                raise CameraPipelineError("preview sample has no buffer")
            jpeg = bytes(buffer.extract_dup(0, buffer.get_size()))
            pipeline_delay_ms = None
            try:
                pts = sample.get_segment().to_running_time(self._gst.Format.TIME, buffer.pts)
                clock = self._pipeline.get_clock()
                if 0 <= pts < 2**63 and clock is not None:
                    pipeline_delay_ms = max(0, round((clock.get_time() - self._pipeline.get_base_time() - pts) / 1e6))
            except (AttributeError, TypeError, ValueError):
                pass  # Optional timing must not interrupt image delivery.
            metadata = {'source_pts_ns': getattr(buffer, 'pts', None)}
            if pipeline_delay_ms is not None:
                metadata['pipeline_delay_ms'] = pipeline_delay_ms
            self._on_preview_jpeg(jpeg, **metadata)
            return self._gst.FlowReturn.OK
        except BaseException:
            self._emit(
                PipelineEvent(
                    PipelineEventKind.ERROR,
                    "JPEG preview delivery failed",
                )
            )
            return self._gst.FlowReturn.ERROR

    def _new_native_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self._gst.FlowReturn.EOS
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        buffer = sample.get_buffer()
        # BGRx is four bytes/pixel; nvvidconv's CPU output has aligned rows.
        data = buffer.extract_dup(0, buffer.get_size())
        self._on_native_frame(data, width, height, len(data) // height,
                              source_pts_ns=getattr(buffer, "pts", None))
        return self._gst.FlowReturn.OK

    def _new_decoded_sample(self, sink: Any) -> Any:
        try:
            if self._null_state_requested:
                return self._gst.FlowReturn.EOS
            sample = sink.emit("pull-sample")
            if sample is None:
                return self._gst.FlowReturn.EOS
            info = self._gst_video.VideoInfo()
            if not info.from_caps(sample.get_caps()):
                raise CameraPipelineError("invalid decoded video layout")
            buffer = sample.get_buffer()
            self._on_decoded_frame(bytes(buffer.extract_dup(0, buffer.get_size())),
                                   info.width, info.height, info.stride[0],
                                   source_pts_ns=getattr(buffer, 'pts', None))
            return self._gst.FlowReturn.OK
        except BaseException:
            self._emit(PipelineEvent(PipelineEventKind.ERROR, "decoded inference delivery failed"))
            return self._gst.FlowReturn.ERROR

    def _bus_message(self, _bus: Any, message: Any, _user_data: Any) -> Any:
        try:
            if message.type == self._gst.MessageType.ERROR:
                error, debug = message.parse_error()
                source = "gstreamer"
                source_object = getattr(message, "src", None)
                source_name = getattr(source_object, "get_name", None)
                if callable(source_name):
                    source = str(source_name()) or source
                detail = f"{source}: {getattr(error, 'message', 'pipeline error')}"
                if debug:
                    detail = f"{detail}; {debug}"
                event = _QueuedBusEvent(
                    kind=PipelineEventKind.ERROR,
                    detail=detail,
                )
            elif message.type == self._gst.MessageType.EOS:
                event = _QueuedBusEvent(kind=PipelineEventKind.EOS)
            elif message.type == self._gst.MessageType.ELEMENT:
                event = self._copy_element_message(message)
            else:
                event = None
        except BaseException:
            event = _QueuedBusEvent(
                kind=PipelineEventKind.ERROR,
                detail="GStreamer bus message handling failed",
            )
        if event is not None:
            self._enqueue_bus_event(event)
        # No GLib main loop drains this bus. Dropping every message after
        # copying bounded primitive data prevents an unbounded GstBus backlog.
        return self._gst.BusSyncReply.DROP

    def _copy_element_message(self, message: Any) -> _QueuedBusEvent | None:
        if self._segment_sink is None or message.src != self._segment_sink:
            return None
        structure = message.get_structure()
        if structure is None:
            return None
        message_name = structure.get_name()
        if message_name not in {
            "splitmuxsink-fragment-opened",
            "splitmuxsink-fragment-closed",
        }:
            return None
        return _QueuedBusEvent(
            kind=PipelineEventKind.FRAGMENT_CLOSED,
            message_name=message_name,
            location=_structure_string(structure, "location"),
            running_time_ns=_structure_uint64(structure, "running-time"),
            duration_ns=_structure_uint64(structure, "duration"),
        )

    def _enqueue_bus_event(self, event: _QueuedBusEvent) -> None:
        with self._bus_state_lock:
            if not self._accept_bus_events:
                # Once EOS and every accepted fragment event have drained,
                # set_state(NULL) can still post duplicate teardown messages
                # before the sync handler is detached. They are outside the
                # accepted generation and do not invalidate confirmed data.
                return
            try:
                self._bus_events.put_nowait(event)
            except queue.Full:
                self._fatal_delivery_error = True
                notify = not self._overflow_notified
                self._overflow_notified = True
            else:
                self._pending_bus_events += 1
                self._drain_complete.clear()
                return
        if notify:
            # Never run an application callback on the GStreamer posting
            # thread, even in the overflow path.
            threading.Thread(
                target=self._emit,
                args=(
                    PipelineEvent(
                        PipelineEventKind.ERROR,
                        "GStreamer event queue overflowed; finalization is unconfirmed",
                    ),
                ),
                name="shore-gstreamer-overflow",
                daemon=True,
            ).start()

    def _run_bus_worker(self) -> None:
        while True:
            try:
                event = self._bus_events.get(timeout=0.1)
            except queue.Empty:
                if self._shutdown_worker.is_set():
                    return
                continue
            try:
                self._process_bus_event(event)
            except BaseException:
                with self._bus_state_lock:
                    self._fatal_delivery_error = True
                self._emit(
                    PipelineEvent(
                        PipelineEventKind.ERROR,
                        "GStreamer event worker failed",
                    )
                )
            finally:
                fragments_are_closed = not self._fragment_boundaries.has_open_fragments
                with self._bus_state_lock:
                    self._pending_bus_events -= 1
                    pending = self._pending_bus_events
                    eos_received = self._eos_received
                    if pending == 0 and eos_received and fragments_are_closed:
                        self._drain_complete.set()
                self._bus_events.task_done()

    def _process_bus_event(self, event: _QueuedBusEvent) -> None:
        if event.kind == PipelineEventKind.ERROR:
            self._emit(
                PipelineEvent(
                    PipelineEventKind.ERROR,
                    self._diagnostic_redactor.redact(
                        event.detail,
                        fallback="GStreamer pipeline error",
                    ),
                )
            )
            return
        if event.kind == PipelineEventKind.EOS:
            with self._bus_state_lock:
                self._eos_received = True
            self._emit(PipelineEvent(PipelineEventKind.EOS, "stream ended"))
            return
        if event.message_name is not None:
            self._fragment_boundaries.receive(
                event.message_name,
                location=event.location,
                running_time_ns=event.running_time_ns,
                duration_ns=event.duration_ns,
            )

    def _emit(self, event: PipelineEvent) -> None:
        if event.kind == PipelineEventKind.ERROR:
            with self._bus_state_lock:
                self._fatal_delivery_error = True
        try:
            self._on_event(event)
        except BaseException:
            with self._bus_state_lock:
                self._fatal_delivery_error = True
            # The worker must never be unwound by an application callback.
            return


@dataclass(frozen=True, slots=True)
class _QueuedBusEvent:
    kind: PipelineEventKind
    detail: str = ""
    message_name: str | None = None
    location: str | None = None
    running_time_ns: int | None = None
    duration_ns: int | None = None


def _structure_uint64(structure: Any, name: str) -> int | None:
    try:
        value = structure.get_uint64(name)
    except (AttributeError, TypeError, ValueError):
        return None
    if isinstance(value, tuple):
        success, number = value
        value = number if success else None
    if value is None or int(value) == (1 << 64) - 1:
        return None
    return int(value)


def _structure_string(structure: Any, name: str) -> str | None:
    try:
        value = structure.get_string(name)
    except (AttributeError, TypeError, ValueError):
        return None
    if isinstance(value, tuple):
        success, text = value
        return str(text) if success and text else None
    return str(value) if value else None


class CameraPipeline:
    """Own a GStreamer pipeline while exposing only secret-free status."""

    def __init__(
        self,
        *,
        status: StatusStore,
        preview: PreviewStore | PreviewSession,
        rtsp_uri: RtspUri,
        spec: PipelineSpec,
        on_fragment_closed: Callable[[ClosedFragment], None],
        factory: PipelineFactory | None = None,
    ) -> None:
        self._status = status
        self._preview = preview
        self._rtsp_uri = rtsp_uri
        self._spec = spec
        self._on_fragment_closed = on_fragment_closed
        self._factory = factory or GStreamerPipelineFactory()
        self._handle: PipelineHandle | None = None
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._event_delivery_lock = threading.RLock()
        self._fragment_handoff_lock = threading.Lock()
        self._stopping = False
        self._stream_confirmed = False
        self._recording_confirmed = False
        self._terminal_event = False
        self._terminal_kind: PipelineEventKind | None = None
        self._last_progress_monotonic: float | None = None
        self._recording_progress_monotonic: float | None = None
        self._active_recording_path: Path | None = None
        self._active_recording_size: int | None = None
        self._finalization_started_monotonic: float | None = None
        self._cleanup_unconfirmed = False
        self._callbacks_active = False
        self._callback_generation = 0
        self._active_callback_generation: int | None = None

    def __repr__(self) -> str:
        return f"CameraPipeline(codec={self._spec.codec.value!r}, uri=[REDACTED])"

    def _update_local_recording(self, state: str, detail: str) -> None:
        if self._spec.recording_pattern is not None:
            self._status.update_component("recording", state, detail)

    @property
    def running(self) -> bool:
        with self._lock:
            return (
                self._handle is not None
                and not self._stopping
                and not self._terminal_event
            )

    @property
    def terminal_kind(self) -> PipelineEventKind | None:
        with self._lock:
            return self._terminal_kind

    def quiesce(self) -> None:
        """Atomically stop accepting readiness-producing callbacks."""

        with self._lock:
            self._stopping = True

    @property
    def cleanup_unconfirmed(self) -> bool:
        with self._lock:
            return self._cleanup_unconfirmed

    @property
    def resources_released(self) -> bool:
        with self._lock:
            return self._handle is None and not self._cleanup_unconfirmed

    @property
    def progress_age_seconds(self) -> float | None:
        with self._lock:
            last_progress = self._last_progress_monotonic
        if last_progress is None:
            return None
        return max(0.0, time.monotonic() - last_progress)

    @property
    def recording_progress_age_seconds(self) -> float | None:
        """Observe independent on-disk progress for the encoded branch."""

        now = time.monotonic()
        with self._lock:
            path = self._active_recording_path
            previous_size = self._active_recording_size
            last_progress = self._recording_progress_monotonic
        if last_progress is None:
            return None
        if path is None:
            return max(0.0, now - last_progress)
        try:
            metadata = path.stat()
        except OSError:
            return float("inf")
        with self._lock:
            if self._active_recording_path != path:
                return max(
                    0.0,
                    now - (self._recording_progress_monotonic or now),
                )
            if previous_size is None or metadata.st_size > previous_size:
                self._active_recording_size = metadata.st_size
                self._recording_progress_monotonic = now
            elif metadata.st_size < previous_size:
                return float("inf")
            if (
                metadata.st_size > 0
                and not self._recording_confirmed
                and not self._stopping
                and not self._terminal_event
            ):
                self._recording_confirmed = True
                self._status.update_component(
                    "recording", "ready", "encoded fragment is growing"
                )
            age = max(0.0, now - (self._recording_progress_monotonic or now))
        return age

    @property
    def finalization_age_seconds(self) -> float | None:
        """Return how long a closed-fragment handoff has been blocking."""

        with self._lock:
            started = self._finalization_started_monotonic
        if started is None:
            return None
        return max(0.0, time.monotonic() - started)

    def start(self) -> bool:
        with self._lifecycle_lock:
            return self._start_serialized()

    def _start_serialized(self) -> bool:
        with self._lock:
            if self._handle is not None:
                return not self._stopping and not self._terminal_event
            if self._cleanup_unconfirmed:
                # A factory that raised before returning a handle may still own
                # callbacks or native resources we cannot stop. Only a fresh
                # process may establish a new generation after that boundary.
                return False
            self._stopping = False
            self._stream_confirmed = False
            self._recording_confirmed = False
            self._terminal_event = False
            self._terminal_kind = None
            self._last_progress_monotonic = time.monotonic()
            self._recording_progress_monotonic = (
                time.monotonic() if self._spec.recording_pattern is not None else None
            )
            self._active_recording_path = None
            self._active_recording_size = None
            self._finalization_started_monotonic = None
            self._cleanup_unconfirmed = False
            self._callbacks_active = True
            self._callback_generation += 1
            callback_generation = self._callback_generation
            self._active_callback_generation = callback_generation
        self._status.update_component("camera", "starting", "waiting for video")
        self._update_local_recording("starting", "waiting for video")
        handle: PipelineHandle | None = None
        factory_invoked = False
        try:
            pattern = self._spec.recording_pattern
            if pattern is not None:
                pattern.parent.mkdir(parents=True, exist_ok=True)
            factory_invoked = True
            handle = self._factory.build(
                self._spec,
                self._rtsp_uri._reveal(),
                lambda jpeg, generation=callback_generation, **metadata: self._receive_preview(
                    generation, jpeg, **metadata
                ),
                lambda event, generation=callback_generation: self._receive_event(
                    generation, event
                ),
            )
            if pattern is not None and _recording_pattern_has_existing_fragment(pattern):
                raise CameraPipelineError(
                    "recording pattern would overwrite an existing fragment"
                )
            if not handle.play():
                raise CameraPipelineError("GStreamer refused the PLAYING state")
            with self._lock:
                terminal = self._terminal_event or self._stopping
            if terminal:
                self._abort_unstarted_handle(handle, callback_generation)
                return False
        except GStreamerUnavailable:
            self._deactivate_failed_start(
                callback_generation,
                cleanup_unconfirmed=False,
            )
            self._status.update_component(
                "camera", "offline", "GStreamer is unavailable"
            )
            self._update_local_recording("paused", "camera pipeline unavailable")
            return False
        except BaseException as exc:
            if handle is not None:
                self._abort_unstarted_handle(handle, callback_generation)
            else:
                self._deactivate_failed_start(
                    callback_generation,
                    cleanup_unconfirmed=factory_invoked,
                )
            detail = self._rtsp_uri.redact(
                exc, fallback="camera pipeline could not start"
            )
            self._status.update_component("camera", "error", detail)
            self._update_local_recording("paused", "camera pipeline did not start")
            return False
        with self._lock:
            if self._terminal_event or self._stopping:
                # A callback won after the prior check but before ownership was
                # published.  Keep the same bounded cleanup contract.
                terminal = True
            else:
                self._handle = handle
                terminal = False
        if terminal:
            self._abort_unstarted_handle(handle, callback_generation)
            return False
        return True

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
        with self._lifecycle_lock:
            return self._stop_serialized(timeout_seconds=timeout_seconds)

    def _stop_serialized(self, *, timeout_seconds: float) -> bool:
        with self._lock:
            handle = self._handle
            self._stopping = True
            hidden_cleanup = self._cleanup_unconfirmed and handle is None
        finalized = not hidden_cleanup
        if handle is not None:
            try:
                finalized = handle.stop(
                    graceful=self._spec.recording_pattern is not None,
                    timeout_seconds=timeout_seconds,
                )
            except BaseException:
                finalized = False
        released = finalized or (handle is not None and bool(
            getattr(handle, "resources_released", False)
        ))
        # Serialize the terminal decision behind any callback that had already
        # entered before stop returned. All later close events either see an
        # inactive successful generation or ERROR after an unconfirmed stop.
        with self._event_delivery_lock:
            with self._lock:
                if released:
                    self._handle = None
                self._stream_confirmed = False
                self._recording_confirmed = False
                if finalized:
                    self._terminal_event = False
                    self._terminal_kind = None
                    self._last_progress_monotonic = None
                    self._recording_progress_monotonic = None
                    self._active_recording_path = None
                    self._active_recording_size = None
                    self._finalization_started_monotonic = None
                    self._cleanup_unconfirmed = False
                    self._callbacks_active = False
                else:
                    self._terminal_event = True
                    self._terminal_kind = PipelineEventKind.ERROR
                    self._cleanup_unconfirmed = not released
                self._callbacks_active = False
                self._status.disarm("camera pipeline stopped")
                self._status.update_component("camera", "offline", "stopped")
                self._update_local_recording(
                    "paused", "stopped" if finalized else "finalization was not confirmed",
                )
        return finalized

    def _abort_unstarted_handle(
        self,
        handle: PipelineHandle,
        callback_generation: int,
    ) -> bool:
        """Quiesce callbacks and retain ownership if bounded cleanup fails."""

        with self._lock:
            self._stopping = True
        try:
            finalized = handle.stop(graceful=False, timeout_seconds=1.0)
        except BaseException:
            finalized = False
        with self._event_delivery_lock:
            with self._lock:
                if self._active_callback_generation != callback_generation:
                    return False
                self._callbacks_active = False
                if not finalized and not getattr(handle, "resources_released", False):
                    self._handle = handle
                    self._terminal_event = True
                    self._terminal_kind = PipelineEventKind.ERROR
                    self._cleanup_unconfirmed = True
        return finalized

    def _deactivate_failed_start(
        self,
        callback_generation: int,
        *,
        cleanup_unconfirmed: bool,
    ) -> None:
        with self._event_delivery_lock:
            with self._lock:
                if self._active_callback_generation != callback_generation:
                    return
                self._stopping = True
                self._callbacks_active = False
                if cleanup_unconfirmed:
                    self._terminal_event = True
                    self._terminal_kind = PipelineEventKind.ERROR
                    self._cleanup_unconfirmed = True

    def _receive_preview(self, generation: int, jpeg: bytes,
                         *, source_pts_ns: int | None = None, pipeline_delay_ms: int | None = None) -> None:
        with self._lock:
            if (
                not self._callbacks_active
                or generation != self._active_callback_generation
                or self._stopping
                or self._terminal_event
            ):
                return
            metadata = {}
            if source_pts_ns is not None:
                metadata['source_pts_ns'] = source_pts_ns
            if pipeline_delay_ms is not None:
                metadata['pipeline_delay_ms'] = pipeline_delay_ms
            self._preview.publish(jpeg, **metadata)
            self._last_progress_monotonic = time.monotonic()
            self._status.update_metrics(camera_frame_age_ms=0)
            if not self._stream_confirmed:
                self._stream_confirmed = True
                self._status.update_component(
                    "camera", "ready", "decoded video is active"
                )

    def _receive_event(self, generation: int, event: PipelineEvent) -> None:
        if self._spec.recording_pattern is None and event.kind in {
            PipelineEventKind.FRAGMENT_OPENED, PipelineEventKind.FRAGMENT_CLOSED,
        }:
            return
        if event.kind == PipelineEventKind.FRAGMENT_CLOSED:
            self._receive_fragment_closed(generation, event)
            return
        with self._event_delivery_lock:
            self._receive_event_serialized(generation, event)

    def _receive_event_serialized(
        self,
        generation: int,
        event: PipelineEvent,
    ) -> None:
        with self._lock:
            if (
                not self._callbacks_active
                or generation != self._active_callback_generation
            ):
                return
        if event.kind == PipelineEventKind.FRAGMENT_OPENED:
            with self._lock:
                if self._stopping or self._terminal_event:
                    return
            opened = event.opened_fragment
            if opened is None or not self._opened_fragment_is_safe(opened):
                self._latch_terminal(PipelineEventKind.ERROR)
                self._status.disarm("recording fragment could not be confirmed")
                self._status.update_component(
                    "recording", "error", "recording fragment open was not confirmed"
                )
                raise CameraPipelineError(
                    "recording fragment open was not confirmed"
                )
            try:
                metadata = opened.path.stat()
            except OSError:
                self._latch_terminal(PipelineEventKind.ERROR)
                self._status.update_component(
                    "recording", "error", "recording fragment disappeared"
                )
                raise CameraPipelineError(
                    "recording fragment disappeared"
                ) from None
            with self._lock:
                if self._stopping or self._terminal_event:
                    return
                self._active_recording_path = opened.path.absolute()
                self._active_recording_size = metadata.st_size
                self._recording_progress_monotonic = time.monotonic()
                if metadata.st_size > 0 and not self._recording_confirmed:
                    self._recording_confirmed = True
                    self._status.update_component(
                        "recording", "ready", "encoded fragment is growing"
                    )
                elif not self._recording_confirmed:
                    self._status.update_component(
                        "recording", "starting", "waiting for encoded bytes"
                    )
            return

        with self._lock:
            if (
                self._terminal_kind is PipelineEventKind.ERROR
                and event.kind is not PipelineEventKind.ERROR
            ):
                # ERROR is the highest-precedence terminal state.  A trailing
                # EOS from teardown must never downgrade its untrusted
                # fragment-delivery boundary.
                return
            stopping = self._stopping
            self._stream_confirmed = False
            self._recording_confirmed = False
            self._terminal_event = True
            if event.kind is PipelineEventKind.ERROR or self._terminal_kind is None:
                self._terminal_kind = event.kind
            if event.kind == PipelineEventKind.EOS:
                detail = "stopped" if stopping else "stream ended"
                self._status.disarm("camera stream ended")
                self._status.update_component("camera", "offline", detail)
                self._update_local_recording("paused", detail)
                return
            if event.kind == PipelineEventKind.ERROR:
                detail = self._rtsp_uri.redact(
                    event.detail, fallback="GStreamer pipeline error"
                )
                self._status.disarm("camera pipeline error")
                self._status.update_component("camera", "error", detail)
                self._update_local_recording("paused", "camera pipeline error")

    def _receive_fragment_closed(
        self,
        generation: int,
        event: PipelineEvent,
    ) -> None:
        # The actual fsync/fingerprint/sidecar handoff can legitimately take
        # longer than the regular recording-growth deadline.  Do not hold the
        # event-delivery lock across it: a bounded stop must still be able to
        # revoke this generation and report unconfirmed cleanup.  The separate
        # handoff lock preserves close-event order for non-GStreamer callers.
        with self._fragment_handoff_lock:
            with self._event_delivery_lock:
                with self._lock:
                    if (
                        not self._callbacks_active
                        or generation != self._active_callback_generation
                        or self._terminal_kind is PipelineEventKind.ERROR
                    ):
                        return
                fragment = event.fragment
                if (
                    fragment is None
                    or fragment.running_time_ns is None
                    or fragment.duration_ns is None
                ):
                    self._latch_terminal(PipelineEventKind.ERROR)
                    self._status.update_component(
                        "recording", "error", "fragment close event was incomplete"
                    )
                    return
                if not _recording_path_matches(
                    self._spec.recording_pattern, fragment.path
                ):
                    self._latch_terminal(PipelineEventKind.ERROR)
                    self._status.update_component(
                        "recording",
                        "error",
                        "closed fragment is outside the configured recording pattern",
                    )
                    return
                with self._lock:
                    self._finalization_started_monotonic = time.monotonic()

            callback_failed = False
            try:
                self._on_fragment_closed(fragment)
            except BaseException:
                callback_failed = True
            finally:
                with self._lock:
                    if generation == self._active_callback_generation:
                        self._finalization_started_monotonic = None

            with self._event_delivery_lock:
                with self._lock:
                    still_active = (
                        self._callbacks_active
                        and generation == self._active_callback_generation
                    )
                if callback_failed:
                    if still_active:
                        self._latch_terminal(PipelineEventKind.ERROR)
                        self._status.update_component(
                            "recording", "error", "fragment handoff callback failed"
                        )
                    raise CameraPipelineError(
                        "fragment handoff callback failed"
                    ) from None
                with self._lock:
                    if (
                        still_active
                        and self._terminal_kind is not PipelineEventKind.ERROR
                        and self._active_recording_path == fragment.path.absolute()
                    ):
                        self._active_recording_path = None
                        self._active_recording_size = None
                        self._recording_progress_monotonic = time.monotonic()

    def _latch_terminal(self, kind: PipelineEventKind) -> None:
        with self._lock:
            self._stream_confirmed = False
            self._recording_confirmed = False
            self._terminal_event = True
            self._terminal_kind = kind

    def _opened_fragment_is_safe(self, opened: OpenedFragment) -> bool:
        path = opened.path.absolute()
        if not _recording_path_matches(self._spec.recording_pattern, path):
            return False
        return path.is_file()
