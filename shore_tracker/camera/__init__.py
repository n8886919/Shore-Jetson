"""Credential-safe camera and recording adapters.

Importing this package never imports PyGObject.  The GStreamer bindings are
loaded only when :class:`CameraPipeline` is started with the real factory.
"""

from .pipeline import (
    CameraPipeline,
    CameraPipelineError,
    ClosedFragment,
    ElementSpec,
    GStreamerPipelineFactory,
    GStreamerUnavailable,
    LinkSpec,
    OpenedFragment,
    PipelineEvent,
    PipelineEventKind,
    PipelineSpec,
    RtspUri,
    VideoCodec,
    build_pipeline_spec,
)

__all__ = [
    "CameraPipeline",
    "CameraPipelineError",
    "ClosedFragment",
    "ElementSpec",
    "GStreamerPipelineFactory",
    "GStreamerUnavailable",
    "LinkSpec",
    "OpenedFragment",
    "PipelineEvent",
    "PipelineEventKind",
    "PipelineSpec",
    "RtspUri",
    "VideoCodec",
    "build_pipeline_spec",
]
