#!/usr/bin/env python3
"""Decode advancing camera frames without credentials or retaining media."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
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
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--provisioner", type=Path, required=True)
    args = parser.parse_args()
    host = str(ipaddress.IPv4Address(args.host))
    if not 1 <= args.frames <= 300 or not 1 <= args.timeout <= 60:
        parser.error("invalid bounded verification limits")

    provisioner = load_provisioner(args.provisioner)
    uri = provisioner.fetch_rtsp_uri(host)

    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstRtsp", "1.0")
    from gi.repository import Gst, GstRtsp

    Gst.init(None)
    pipeline = Gst.Pipeline.new("camera-decode-verification")
    names = (
        ("rtspsrc", "source"),
        ("rtph265depay", "depay"),
        ("h265parse", "parser"),
        ("nvv4l2decoder", "decoder"),
        ("fakesink", "sink"),
    )
    elements = {}
    for factory, name in names:
        element = Gst.ElementFactory.make(factory, name)
        if element is None:
            raise RuntimeError("required GStreamer element unavailable")
        pipeline.add(element)
        elements[name] = element
    elements["source"].set_property("location", uri)
    elements["source"].set_property("protocols", GstRtsp.RTSPLowerTrans.TCP)
    elements["source"].set_property("latency", 250)
    elements["sink"].set_property("sync", False)
    elements["sink"].set_property("signal-handoffs", True)
    if not (
        elements["depay"].link(elements["parser"])
        and elements["parser"].link(elements["decoder"])
        and elements["decoder"].link(elements["sink"])
    ):
        raise RuntimeError("GStreamer verification pipeline could not link")

    state = {"frames": 0, "linked": False}

    def on_pad_added(_source, pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or caps.get_size() < 1:
            return
        structure = caps.get_structure(0)
        if (
            structure.get_string("media") != "video"
            or structure.get_string("encoding-name") != "H265"
        ):
            return
        sink_pad = elements["depay"].get_static_pad("sink")
        if sink_pad is not None and not sink_pad.is_linked():
            state["linked"] = pad.link(sink_pad) == Gst.PadLinkReturn.OK

    def on_handoff(_sink, _buffer, _pad) -> None:
        state["frames"] += 1

    elements["source"].connect("pad-added", on_pad_added)
    elements["sink"].connect("handoff", on_handoff)
    bus = pipeline.get_bus()
    deadline = time.monotonic() + args.timeout
    result = "TIMEOUT"
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            result = "FAIL"
        else:
            while time.monotonic() < deadline and state["frames"] < args.frames:
                message = bus.timed_pop_filtered(
                    250 * Gst.MSECOND,
                    Gst.MessageType.ERROR | Gst.MessageType.EOS,
                )
                if message is not None:
                    result = "FAIL"
                    break
            else:
                if state["frames"] >= args.frames:
                    result = "PASS"
    finally:
        pipeline.set_state(Gst.State.NULL)
        uri = ""
    print(
        f"unauthenticated_decode={result} linked={str(state['linked']).lower()} "
        f"advancing_frames={state['frames']} retained_frames=0"
    )
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
