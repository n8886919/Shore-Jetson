#!/usr/bin/env python3
"""Run the production GStreamer graph with bounded, secret-free diagnostics."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import threading
import time
from dataclasses import replace
from pathlib import Path

from shore_tracker.camera.pipeline import (
    GStreamerPipelineFactory,
    PipelineEvent,
    PipelineEventKind,
    build_pipeline_spec,
)


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
    parser.add_argument("--output-pattern", type=Path, required=True)
    parser.add_argument("--provisioner", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument(
        "--mode", choices=("full", "preview", "recording"), default="full"
    )
    args = parser.parse_args()
    host = str(ipaddress.IPv4Address(args.host))
    pattern = args.output_pattern.absolute()
    if not 2 <= args.seconds <= 60:
        parser.error("seconds must be between 2 and 60")
    pattern.parent.mkdir(parents=True, exist_ok=True)

    uri = load_provisioner(args.provisioner).fetch_rtsp_uri(host)
    spec = build_pipeline_spec("h265", pattern, preview_fps=5)
    if args.mode == "preview":
        spec = replace(
            spec,
            links=tuple(
                link
                for link in spec.links
                if link.source not in {"record_queue"}
                and link.target not in {"record_queue", "segment_sink"}
            ),
        )
    elif args.mode == "recording":
        spec = replace(
            spec,
            links=tuple(
                link
                for link in spec.links
                if not link.source.startswith("preview_")
                and not link.target.startswith("preview_")
            ),
        )
    lock = threading.Lock()
    state = {"frames": 0, "error": False, "eos": False, "opened": 0, "closed": 0}

    def on_preview(jpeg: bytes) -> None:
        if not (jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")):
            state["error"] = True
            return
        with lock:
            state["frames"] += 1

    def on_event(event: PipelineEvent) -> None:
        with lock:
            if event.kind is PipelineEventKind.ERROR:
                state["error"] = True
                print(f"event=error detail={event.detail}", flush=True)
            elif event.kind is PipelineEventKind.EOS:
                state["eos"] = True
                print("event=eos", flush=True)
            elif event.kind is PipelineEventKind.FRAGMENT_OPENED:
                state["opened"] += 1
                print("event=fragment_opened", flush=True)
            elif event.kind is PipelineEventKind.FRAGMENT_CLOSED:
                state["closed"] += 1
                print("event=fragment_closed", flush=True)

    handle = GStreamerPipelineFactory().build(spec, uri, on_preview, on_event)
    uri = ""
    played = handle.play()
    print(f"play={str(played).lower()}", flush=True)
    deadline = time.monotonic() + args.seconds
    while played and time.monotonic() < deadline:
        with lock:
            terminal = state["error"] or state["eos"]
        if terminal:
            break
        time.sleep(0.1)
    stopped = handle.stop(
        graceful=args.mode != "preview", timeout_seconds=10.0
    )
    with lock:
        print(
            "diagnostic "
            f"frames={state['frames']} opened={state['opened']} closed={state['closed']} "
            f"error={str(state['error']).lower()} eos={str(state['eos']).lower()} "
            f"stop_confirmed={str(stopped).lower()}",
            flush=True,
        )
    print(
        "stop_debug "
        f"fatal={str(getattr(handle, '_fatal_delivery_error', None)).lower()} "
        f"eos_received={str(getattr(handle, '_eos_received', None)).lower()} "
        f"pending={getattr(handle, '_pending_bus_events', None)} "
        f"worker_alive={str(getattr(handle, '_bus_worker').is_alive()).lower()} "
        f"open_fragments={str(getattr(handle, '_fragment_boundaries').has_open_fragments).lower()}",
        flush=True,
    )
    progressed = (
        state["frames"] > 0 if args.mode != "recording" else state["opened"] > 0
    )
    return 0 if played and not state["error"] and progressed else 1


if __name__ == "__main__":
    raise SystemExit(main())
