"""Benchmark real TensorRT engines, optionally using live, unrecorded RTSP frames.

No UART, camera settings, filesink, recording service, or tracking commands are
created. Engine files and aggregate JSON metrics are the only retained output.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import shlex
import threading
import time

import numpy as np
from PIL import Image

from shore_tracker.inference.tensorrt_runtime import (
    TensorRtSurfTrackModels, decode_fcos_outputs, prepare_action_input,
    MAX_CANDIDATES, _nms, _sigmoid,
)
from shore_tracker.inference.tracker import IoUTracker, RawDetection
from shore_tracker.inference.types import NormalizedBox, TrackObservation, ActionPrediction


def tile_origins(length: int, size: int, overlap: int) -> tuple[int, ...]:
    if length <= 0 or not 0 <= overlap < size:
        raise ValueError("invalid tile dimensions")
    if length <= size:
        return (0,)
    positions = list(range(0, length - size + 1, size - overlap))
    if positions[-1] != length - size:
        positions.append(length - size)
    return tuple(positions)


class NativeTiledModels:
    """Benchmark-only 1:1 pixel tiles, global NMS, full-resolution action crops."""
    def __init__(self, models):
        self.models = models
        self.tracker = IoUTracker(match_iou=.25, grace_seconds=5)
        self.tile_count = 0

    def infer_jpeg(self, jpeg):
        with Image.open(io.BytesIO(jpeg)) as source:
            image = source.convert("RGB")
        return self.infer_image(image)

    def infer_image(self, image):
        xs, ys = tile_origins(image.width, 896, 128), tile_origins(image.height, 512, 64)
        self.tile_count = len(xs) * len(ys)
        candidates = []
        for top in ys:
            for left in xs:
                crop = image.crop((left, top, min(left + 896, image.width), min(top + 512, image.height)))
                canvas = np.empty((512, 896, 3), np.float32)
                canvas[:] = (123.675, 116.28, 103.53)
                canvas[:crop.height, :crop.width] = np.asarray(crop, dtype=np.float32)
                tensor = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None])
                raw = decode_fcos_outputs(self.models.detector.infer(tensor),
                    resized_width=crop.width, resized_height=crop.height,
                    score_threshold=self.models.score_threshold)
                for item in raw:
                    b = item.box
                    candidates.append(RawDetection(NormalizedBox(
                        (left + b.x * crop.width) / image.width,
                        (top + b.y * crop.height) / image.height,
                        b.width * crop.width / image.width,
                        b.height * crop.height / image.height), item.confidence))
        raw = tuple(_nms(candidates, .60, MAX_CANDIDATES))
        detections = self.tracker.update(raw)
        if not detections:
            return ()
        probabilities = _sigmoid(self.models.action.infer(prepare_action_input(image, raw))["action_logits"]).tolist()
        if len(probabilities) != len(detections):
            raise RuntimeError("action output count does not match detections")
        return tuple(TrackObservation(d, ActionPrediction(d.track_id, *p))
                     for d, p in zip(detections, probabilities))


class ActionLoadModels:
    """Add a clearly labelled fixed crop batch; never fabricate detections."""

    def __init__(self, models, count):
        self.models = models
        self.extra_action_crops = count

    def infer_jpeg(self, jpeg):
        with Image.open(io.BytesIO(jpeg)) as source:
            image = source.convert("RGB")
        return self.infer_image(image)

    def infer_image(self, image):
        observations = self.models.infer_image(image)
        crops = tuple(RawDetection(NormalizedBox(
            (i % 5) * .2, (i // 5) * .2, .15, .15), 1.0)
            for i in range(self.extra_action_crops))
        output = self.models.action.infer(prepare_action_input(image, crops))
        if not all(np.isfinite(value).all() for value in output.values()):
            raise RuntimeError("additional action batch returned non-finite output")
        return observations


def timing_summary(seconds: list[float], elapsed: float | None = None) -> dict:
    if not seconds or any(t <= 0 or not np.isfinite(t) for t in seconds):
        raise ValueError("timing samples must be finite and positive")
    elapsed = sum(seconds) if elapsed is None else elapsed
    return {
        "samples": len(seconds), "elapsed_seconds": elapsed,
        "fps": len(seconds) / elapsed,
        "mean_ms": float(np.mean(seconds) * 1000),
        "p50_ms": float(np.percentile(seconds, 50) * 1000),
        "p95_ms": float(np.percentile(seconds, 95) * 1000),
    }


def benchmark_runner(runner, tensor: np.ndarray, samples: int) -> dict:
    for _ in range(3):
        runner.infer(tensor)
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        outputs = runner.infer(tensor)
        timings.append(time.perf_counter() - started)
        if not all(np.isfinite(value).all() for value in outputs.values()):
            raise RuntimeError("model returned non-finite output")
    return {**timing_summary(timings), "input_shape": list(tensor.shape),
            "output_shapes": {key: list(value.shape) for key, value in outputs.items()}}


class LiveFrames:
    """A bounded latest-JPEG slot; slow inference never queues old frames."""

    def __init__(self, env_file: Path, interface: str, codec: str, frame_size="native", frame_fps=20, transport="jpeg"):
        from shore_tracker.camera.pipeline import RtspUri
        from shore_tracker.runtime import validate_direct_camera_route
        if env_file.stat().st_mode & 0o077:
            raise ValueError("credential file must have mode 0600")
        values = {}
        for line in env_file.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            parsed = shlex.split(value)
            if len(parsed) != 1:
                raise ValueError("invalid credential file")
            values[key] = parsed[0]
        uri = RtspUri.from_environment("SHORE_TRACKER_RTSP_URI", environ=values)
        validate_direct_camera_route(uri, interface)
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        Gst.debug_set_active(False)
        self.gst = Gst
        self.condition = threading.Condition()
        self.sequence = 0
        self.jpeg = b""
        self.acquired_at = 0.0
        self.first_pts = None
        self.last_pts = None
        self.resolution = None
        self.frame_fps = frame_fps
        self.transport = transport
        raw_caps = "video/x-raw" + (",width=960,height=540" if frame_size == "960x540" else "")
        tail = '! videoconvert ! jpegenc quality=80 '
        if transport == "rgba":
            gi.require_version("GstVideo", "1.0")
            from gi.repository import GstVideo
            self.gst_video = GstVideo
            raw_caps += ",format=RGBA"
            tail = ""
        self.pipeline = Gst.parse_launch(
            'rtspsrc name=source protocols=tcp latency=250 drop-on-latency=true '
            f'! rtp{codec}depay ! {codec}parse ! nvv4l2decoder name=decoder '
            '! queue leaky=downstream max-size-buffers=2 max-size-bytes=0 max-size-time=0 '
            '! videorate drop-only=true '
            f'! video/x-raw(memory:NVMM),framerate={frame_fps}/1 '
            f'! nvvidconv ! {raw_caps} '
            f'{tail}'
            '! appsink name=frames emit-signals=true max-buffers=1 drop=true sync=false wait-on-eos=false'
        )
        self.pipeline.get_by_name("source").set_property("location", uri._reveal())
        self.pipeline.get_by_name("frames").connect("new-sample", self._sample)
        self.bus = self.pipeline.get_bus()

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        with self.condition:
            caps = sample.get_caps().get_structure(0)
            self.resolution = [caps.get_value("width"), caps.get_value("height")]
            self.jpeg = buffer.extract_dup(0, buffer.get_size())
            if self.transport == "rgba":
                info = self.gst_video.VideoInfo()
                if not info.from_caps(sample.get_caps()):
                    return self.gst.FlowReturn.ERROR
                self.jpeg = (self.jpeg, *self.resolution, info.stride[0])
            self.sequence += 1
            self.acquired_at = time.monotonic()
            if self.first_pts is None:
                self.first_pts = buffer.pts
            self.last_pts = buffer.pts
            self.condition.notify_all()
        return self.gst.FlowReturn.OK

    def start(self):
        if self.pipeline.set_state(self.gst.State.PLAYING) == self.gst.StateChangeReturn.FAILURE:
            raise RuntimeError("camera pipeline could not start")

    def newest(self, after: int):
        deadline = time.monotonic() + 15
        with self.condition:
            while self.sequence <= after:
                if self.bus.pop_filtered(self.gst.MessageType.ERROR):
                    # Never propagate a GStreamer error containing a stream URI.
                    raise RuntimeError("camera decode failed; no stream URL logged")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError("no advancing decoded frame within 15 seconds")
                self.condition.wait(min(left, 0.5))
            return self.sequence, self.jpeg, self.acquired_at

    def close(self):
        self.pipeline.set_state(self.gst.State.NULL)
        with self.condition:
            self.jpeg = b""


def infer_frame(models, payload, transport):
    if transport == "rgba":
        data, width, height, stride = payload
        image = Image.frombytes("RGB", (width, height), data, "raw", "RGBX", stride)
        return models.infer_image(image)
    return models.infer_jpeg(payload)


def benchmark_live(models, frames, samples, warmups=3):
    frames.start()
    timings, ages, counts = [], [], []
    sequence = 0
    try:
        for _ in range(warmups):
            sequence, jpeg, _ = frames.newest(sequence)
            infer_frame(models, jpeg, frames.transport)
        initial_sequence = frames.sequence
        started = time.perf_counter()
        for _ in range(samples):
            sequence, jpeg, acquired_at = frames.newest(sequence)
            tick = time.perf_counter()
            observations = infer_frame(models, jpeg, frames.transport)
            timings.append(time.perf_counter() - tick)
            ages.append(time.monotonic() - acquired_at)
            counts.append(len(observations))
        elapsed = time.perf_counter() - started
        if frames.first_pts == frames.last_pts:
            raise RuntimeError("camera timestamps did not advance")
        return {
            **timing_summary(timings, elapsed),
            "preview_fps": (frames.sequence - initial_sequence) / elapsed,
            "detections_min": min(counts), "detections_max": max(counts),
            "detections_mean": float(np.mean(counts)),
            "completed_frame_age_p95_ms": float(np.percentile(ages, 95) * 1000),
            "decoded_frame_size": frames.resolution, "rate_limit": None,
            "frame_delivery": frames.transport,
            "decoded_frame_rate_limit": frames.frame_fps,
            "detector_tiles_per_frame": getattr(models, "tile_count", 1),
            "additional_action_crops_per_frame": getattr(models, "extra_action_crops", 0),
            "detector_input_per_pass": [896, 512],
            "recording": False, "servo": False,
        }
    finally:
        frames.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--interface", default="eth0")
    parser.add_argument("--codec", choices=("h264", "h265"), default="h265")
    parser.add_argument("--frame-size", choices=("native", "960x540"), default="native")
    parser.add_argument("--frame-fps", type=int, default=20,
                        help="Drop excess decoded frames before CPU conversion/JPEG; preserve resolution")
    parser.add_argument("--transport", choices=("jpeg", "rgba"), default="jpeg")
    parser.add_argument("--extra-action-crops", type=int, choices=(0, 2, 4, 5, 10, 20), default=0,
                        help="Additional simulated action workload; does not create detections")
    parser.add_argument("--native-tiles", action="store_true")
    parser.add_argument("--tile-samples", type=int, default=5)
    parser.add_argument("--live-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10000:
        parser.error("samples must be between 1 and 10000")
    if not 1 <= args.tile_samples <= 10000:
        parser.error("tile-samples must be between 1 and 10000")
    if not 1 <= args.frame_fps <= 20:
        parser.error("frame-fps must be between 1 and 20")
    if (args.live_only or args.native_tiles) and not args.env_file:
        parser.error("live options require an env-file")
    if args.extra_action_crops and (not args.env_file or args.native_tiles):
        parser.error("extra action workload requires live input without native-tiles")
    os.umask(0o077)
    models = TensorRtSurfTrackModels(
        args.models / "surfer_fcos_raw.onnx",
        args.models / "actions_mobilenet_v3_small.onnx",
        args.models / "engines",
    )
    started = time.perf_counter()
    models.load()
    result = {"engine_load_seconds": time.perf_counter() - started,
              "scope": "synthetic_input_model_latency_not_live_fps"}
    print("engines_ready", flush=True)
    if not args.live_only:
        result["detector"] = benchmark_runner(models.detector, np.zeros((1, 3, 512, 896), np.float32), args.samples)
        print("detector", json.dumps(result["detector"]), flush=True)
        result["action"] = {}
        for batch in (1, 2, 4, 5, 10, 20):
            measured = benchmark_runner(models.action, np.zeros((batch, 3, 160, 160), np.float32), args.samples)
            measured["batches_per_second"] = measured.pop("fps")
            measured["crops_per_second"] = batch * measured["batches_per_second"]
            result["action"][str(batch)] = measured
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the valid model timings if a subsequent live-camera test fails.
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.env_file:
        live_models = ActionLoadModels(models, args.extra_action_crops) if args.extra_action_crops else models
        result["live"] = benchmark_live(live_models, LiveFrames(args.env_file, args.interface, args.codec, args.frame_size, args.frame_fps, args.transport), args.samples)
        result["scope"] = "model_latency_and_live_camera_pipeline"
        print("live", json.dumps(result["live"]), flush=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        if args.native_tiles:
            result["native_tiles"] = benchmark_live(NativeTiledModels(models),
                LiveFrames(args.env_file, args.interface, args.codec, "native", args.frame_fps, args.transport), args.tile_samples, warmups=1)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
