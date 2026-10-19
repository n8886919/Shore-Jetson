#!/usr/bin/env python3
"""Capture one authorized JPEG from the direct camera stream."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import os
import time
from pathlib import Path


def load_provisioner(path: Path):
    spec = importlib.util.spec_from_file_location("camera_provision", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("camera provisioner unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provisioner", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    host = str(ipaddress.IPv4Address(args.host))
    output = args.output.absolute()
    if output.exists() or output.is_symlink() or not 1 <= args.timeout <= 60:
        parser.error("unsafe output or timeout")

    uri = load_provisioner(args.provisioner).fetch_rtsp_uri(host)

    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstRtsp", "1.0")
    from gi.repository import Gst, GstRtsp

    Gst.init(None)
    pipeline = Gst.Pipeline.new("authorized-camera-frame")
    specifications = (
        ("rtspsrc", "source"),
        ("rtph265depay", "depay"),
        ("h265parse", "parser"),
        ("nvv4l2decoder", "decoder"),
        ("nvvidconv", "converter"),
        ("capsfilter", "caps"),
        ("videoconvert", "colorspace"),
        ("jpegenc", "jpeg"),
        ("appsink", "sink"),
    )
    elements = {}
    for factory, name in specifications:
        element = Gst.ElementFactory.make(factory, name)
        if element is None:
            raise RuntimeError("required GStreamer element unavailable")
        pipeline.add(element)
        elements[name] = element

    elements["source"].set_property("location", uri)
    elements["source"].set_property("protocols", GstRtsp.RTSPLowerTrans.TCP)
    elements["source"].set_property("latency", 250)
    elements["caps"].set_property(
        "caps", Gst.Caps.from_string("video/x-raw,width=960,height=540")
    )
    elements["jpeg"].set_property("quality", 90)
    elements["sink"].set_property("emit-signals", True)
    elements["sink"].set_property("max-buffers", 1)
    elements["sink"].set_property("drop", True)
    elements["sink"].set_property("sync", False)
    ordered = ("depay", "parser", "decoder", "converter", "caps", "colorspace", "jpeg", "sink")
    if not all(elements[left].link(elements[right]) for left, right in zip(ordered, ordered[1:])):
        raise RuntimeError("capture pipeline could not link")

    state = {"linked": False, "seen": 0, "jpeg": None}

    def on_pad_added(_source, pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or caps.get_size() < 1:
            return
        structure = caps.get_structure(0)
        if structure.get_string("media") != "video" or structure.get_string("encoding-name") != "H265":
            return
        sink_pad = elements["depay"].get_static_pad("sink")
        if sink_pad is not None and not sink_pad.is_linked():
            state["linked"] = pad.link(sink_pad) == Gst.PadLinkReturn.OK

    def on_sample(sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        state["seen"] += 1
        if state["seen"] < 3 or state["jpeg"] is not None:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            state["jpeg"] = bytes(mapped.data)
        finally:
            buffer.unmap(mapped)
        return Gst.FlowReturn.OK

    elements["source"].connect("pad-added", on_pad_added)
    elements["sink"].connect("new-sample", on_sample)
    bus = pipeline.get_bus()
    deadline = time.monotonic() + args.timeout
    failed = False
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            failed = True
        while not failed and state["jpeg"] is None and time.monotonic() < deadline:
            message = bus.timed_pop_filtered(
                250 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS
            )
            failed = message is not None
    finally:
        pipeline.set_state(Gst.State.NULL)
        uri = ""
    jpeg = state["jpeg"]
    if failed or not state["linked"] or not isinstance(jpeg, bytes):
        print("camera_capture=FAIL")
        return 1
    if not (jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")):
        print("camera_capture=FAIL")
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(jpeg)
    print(f"camera_capture=PASS bytes={len(jpeg)} frames_seen={state['seen']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
