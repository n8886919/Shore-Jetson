from __future__ import annotations

import ctypes
import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from shore_tracker.inference.tracker import IoUTracker, RawDetection, box_iou
from shore_tracker.inference.types import (
    ActionPrediction,
    NormalizedBox,
    TrackObservation,
)


DETECTOR_WIDTH = 896
DETECTOR_HEIGHT = 512
ACTION_SIZE = 160
MAX_CANDIDATES = 10
FEATURE_LEVELS = (
    (64, 112, 8),
    (32, 56, 16),
    (16, 28, 32),
    (8, 14, 64),
    (4, 7, 128),
)


class TensorRtUnavailable(RuntimeError):
    pass


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def prepare_detector_input(image: Image.Image) -> tuple[np.ndarray, int, int]:
    """Top-left letterbox exactly as the exported detector contract specifies."""

    rgb = image if image.mode == "RGB" else image.convert("RGB")
    scale = min(DETECTOR_WIDTH / rgb.width, DETECTOR_HEIGHT / rgb.height)
    width = max(1, min(DETECTOR_WIDTH, int(round(rgb.width * scale))))
    height = max(1, min(DETECTOR_HEIGHT, int(round(rgb.height * scale))))
    resized = rgb.resize((width, height), Image.Resampling.BILINEAR)
    canvas = np.empty((DETECTOR_HEIGHT, DETECTOR_WIDTH, 3), dtype=np.float32)
    canvas[:] = (123.675, 116.28, 103.53)
    canvas[:height, :width] = np.asarray(resized, dtype=np.float32)
    return np.ascontiguousarray(canvas.transpose(2, 0, 1)[None]), width, height


def _nms(items: list[RawDetection], threshold: float, limit: int) -> list[RawDetection]:
    remaining = sorted(items, key=lambda item: item.confidence, reverse=True)
    kept: list[RawDetection] = []
    while remaining and len(kept) < limit:
        best = remaining.pop(0)
        kept.append(best)
        remaining = [
            item for item in remaining if box_iou(best.box, item.box) <= threshold
        ]
    return kept


def decode_fcos_outputs(
    outputs: dict[str, np.ndarray],
    *,
    resized_width: int,
    resized_height: int,
    score_threshold: float,
    nms_threshold: float = 0.60,
    limit: int = MAX_CANDIDATES,
) -> tuple[RawDetection, ...]:
    cls = np.asarray(outputs["cls_logits"], dtype=np.float32).reshape(-1)
    boxes = np.asarray(outputs["bbox_regression"], dtype=np.float32).reshape(-1, 4)
    ctr = np.asarray(outputs["bbox_ctrness"], dtype=np.float32).reshape(-1)
    expected = sum(height * width for height, width, _stride in FEATURE_LEVELS)
    if cls.size != expected or ctr.size != expected or boxes.shape != (expected, 4):
        raise ValueError("unexpected FCOS output shapes")
    if not 0 < resized_width <= DETECTOR_WIDTH or not 0 < resized_height <= DETECTOR_HEIGHT:
        raise ValueError("invalid detector resize")

    scores = np.sqrt(_sigmoid(cls) * _sigmoid(ctr))
    candidates: list[RawDetection] = []
    offset = 0
    for level_height, level_width, stride in FEATURE_LEVELS:
        count = level_height * level_width
        level_scores = scores[offset : offset + count]
        indices = np.flatnonzero(level_scores > score_threshold)
        if indices.size > 1000:
            top = np.argpartition(level_scores[indices], -1000)[-1000:]
            indices = indices[top]
        for local_index in indices:
            row, column = divmod(int(local_index), level_width)
            # The export contract already uses torchvision's anchor-centre
            # convention and records these flattened centres as column*stride.
            center_x = column * stride
            center_y = row * stride
            left, top, right, bottom = boxes[offset + local_index]
            x1 = max(0.0, center_x - float(left) * stride)
            y1 = max(0.0, center_y - float(top) * stride)
            x2 = min(float(resized_width), center_x + float(right) * stride)
            y2 = min(float(resized_height), center_y + float(bottom) * stride)
            if x2 <= x1 or y2 <= y1:
                continue
            candidates.append(
                RawDetection(
                    NormalizedBox(
                        x1 / resized_width,
                        y1 / resized_height,
                        (x2 - x1) / resized_width,
                        (y2 - y1) / resized_height,
                    ),
                    float(level_scores[local_index]),
                )
            )
        offset += count
    return tuple(_nms(candidates, nms_threshold, limit))


def prepare_action_input(
    image: Image.Image, detections: tuple[RawDetection, ...]
) -> np.ndarray:
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    crops: list[np.ndarray] = []
    for detection in detections:
        box = detection.box
        left = box.x * rgb.width
        top = box.y * rgb.height
        right = (box.x + box.width) * rgb.width
        bottom = (box.y + box.height) * rgb.height
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2
        side = max(right - left, bottom - top) * 1.55
        crop = rgb.crop(
            (
                max(0.0, center_x - side / 2),
                max(0.0, center_y - side / 2),
                min(float(rgb.width), center_x + side / 2),
                min(float(rgb.height), center_y + side / 2),
            )
        ).resize((ACTION_SIZE, ACTION_SIZE), Image.Resampling.BILINEAR)
        crops.append(np.asarray(crop, dtype=np.float32).transpose(2, 0, 1))
    if not crops:
        return np.empty((0, 3, ACTION_SIZE, ACTION_SIZE), dtype=np.float32)
    return np.ascontiguousarray(np.stack(crops))


class _CudaDriver:
    """Minimal CUDA driver binding; avoids PyCUDA and its conflicting toolkit."""

    def __init__(self) -> None:
        try:
            self.lib = ctypes.CDLL("libcuda.so.1")
        except OSError as exc:
            raise TensorRtUnavailable("CUDA driver is unavailable") from exc
        self._configure()
        self._check(self.lib.cuInit(0), "cuInit")
        device = ctypes.c_int()
        self._check(self.lib.cuDeviceGet(ctypes.byref(device), 0), "cuDeviceGet")
        self.context = ctypes.c_void_p()
        self._check(
            self.lib.cuDevicePrimaryCtxRetain(ctypes.byref(self.context), device),
            "cuDevicePrimaryCtxRetain",
        )
        self._check(self.lib.cuCtxSetCurrent(self.context), "cuCtxSetCurrent")
        self.stream = ctypes.c_void_p()
        self._check(
            self.lib.cuStreamCreate(ctypes.byref(self.stream), 0), "cuStreamCreate"
        )

    def _configure(self) -> None:
        self.lib.cuInit.argtypes = [ctypes.c_uint]
        self.lib.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        self.lib.cuDevicePrimaryCtxRetain.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_int,
        ]
        self.lib.cuCtxSetCurrent.argtypes = [ctypes.c_void_p]
        self.lib.cuStreamCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint]
        self.lib.cuMemAlloc_v2.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t]
        self.lib.cuMemFree_v2.argtypes = [ctypes.c_uint64]
        self.lib.cuMemcpyHtoDAsync_v2.argtypes = [
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
        ]
        self.lib.cuMemcpyDtoHAsync_v2.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_size_t,
            ctypes.c_void_p,
        ]
        self.lib.cuStreamSynchronize.argtypes = [ctypes.c_void_p]

    @staticmethod
    def _check(code: int, operation: str) -> None:
        if code != 0:
            raise TensorRtUnavailable(f"{operation} failed with CUDA status {code}")

    def make_current(self) -> None:
        self._check(self.lib.cuCtxSetCurrent(self.context), "cuCtxSetCurrent")

    def allocate(self, size: int) -> int:
        pointer = ctypes.c_uint64()
        self._check(self.lib.cuMemAlloc_v2(ctypes.byref(pointer), size), "cuMemAlloc")
        return int(pointer.value)

    def free(self, pointer: int) -> None:
        self._check(self.lib.cuMemFree_v2(pointer), "cuMemFree")

    def copy_to_device(self, pointer: int, array: np.ndarray) -> None:
        self._check(
            self.lib.cuMemcpyHtoDAsync_v2(
                pointer, ctypes.c_void_p(array.ctypes.data), array.nbytes, self.stream
            ),
            "cuMemcpyHtoDAsync",
        )

    def copy_from_device(self, array: np.ndarray, pointer: int) -> None:
        self._check(
            self.lib.cuMemcpyDtoHAsync_v2(
                ctypes.c_void_p(array.ctypes.data), pointer, array.nbytes, self.stream
            ),
            "cuMemcpyDtoHAsync",
        )

    def synchronize(self) -> None:
        self._check(self.lib.cuStreamSynchronize(self.stream), "cuStreamSynchronize")


class TensorRtRunner:
    def __init__(
        self,
        model_path: Path,
        engine_path: Path,
        *,
        dynamic_batch: bool,
        cuda: _CudaDriver,
    ) -> None:
        self.model_path = Path(model_path)
        self.engine_path = Path(engine_path)
        self.dynamic_batch = dynamic_batch
        self.cuda = cuda
        self.trt: Any = None
        self.engine: Any = None
        self.context: Any = None
        self.runtime: Any = None

    def load(self) -> None:
        try:
            import tensorrt as trt
        except (ImportError, OSError) as exc:
            raise TensorRtUnavailable("TensorRT Python bindings are unavailable") from exc
        self.trt = trt
        logger = trt.Logger(trt.Logger.WARNING)
        trt.init_libnvinfer_plugins(logger, "")
        serialized: bytes | None = None
        if self.engine_path.is_file():
            serialized = self.engine_path.read_bytes()
        if serialized is None:
            serialized = self._build(logger)
            self.engine_path.parent.mkdir(parents=True, exist_ok=True)
            self.engine_path.write_bytes(serialized)
        self.runtime = trt.Runtime(logger)
        self.engine = self.runtime.deserialize_cuda_engine(serialized)
        if self.engine is None:
            raise TensorRtUnavailable("TensorRT engine could not be loaded")
        self.context = self.engine.create_execution_context()
        if self.context is None:
            raise TensorRtUnavailable("TensorRT execution context could not be created")

    def _build(self, logger: Any) -> bytes:
        trt = self.trt
        builder = trt.Builder(logger)
        network = builder.create_network(
            1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
        )
        parser = trt.OnnxParser(network, logger)
        if not parser.parse(self.model_path.read_bytes()):
            detail = str(parser.get_error(0)) if parser.num_errors else "unknown parser error"
            raise TensorRtUnavailable(f"ONNX parse failed: {detail}")
        config = builder.create_builder_config()
        if hasattr(config, "set_memory_pool_limit"):
            config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 30)
        else:
            # JetPack 4 / TensorRT 8.2: leave room for the desktop and decoder
            # on the 4 GB Nano while selecting tactics for each layer.
            config.max_workspace_size = 1 << 28
        if builder.platform_has_fast_fp16:
            config.set_flag(trt.BuilderFlag.FP16)
        if self.dynamic_batch:
            profile = builder.create_optimization_profile()
            name = network.get_input(0).name
            profile.set_shape(
                name,
                (1, 3, ACTION_SIZE, ACTION_SIZE),
                (8, 3, ACTION_SIZE, ACTION_SIZE),
                (20, 3, ACTION_SIZE, ACTION_SIZE),
            )
            config.add_optimization_profile(profile)
        plan = builder.build_serialized_network(network, config)
        if plan is None:
            raise TensorRtUnavailable("TensorRT engine build failed")
        return bytes(plan)

    def infer(self, input_array: np.ndarray) -> dict[str, np.ndarray]:
        if self.engine is None or self.context is None:
            raise RuntimeError("TensorRT runner is not loaded")
        self.cuda.make_current()
        if not hasattr(self.engine, "num_io_tensors"):
            return self._infer_bindings(input_array)
        trt = self.trt
        names = [self.engine.get_tensor_name(i) for i in range(self.engine.num_io_tensors)]
        inputs = [
            name
            for name in names
            if self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT
        ]
        if len(inputs) != 1:
            raise TensorRtUnavailable("model must have exactly one input")
        input_name = inputs[0]
        contiguous = np.ascontiguousarray(input_array)
        if not self.context.set_input_shape(input_name, contiguous.shape):
            raise TensorRtUnavailable("TensorRT rejected the input shape")
        host: dict[str, np.ndarray] = {input_name: contiguous}
        for name in names:
            if name == input_name:
                continue
            shape = tuple(self.context.get_tensor_shape(name))
            if any(dimension < 0 for dimension in shape):
                raise TensorRtUnavailable("TensorRT output shape is unresolved")
            host[name] = np.empty(shape, dtype=trt.nptype(self.engine.get_tensor_dtype(name)))
        device: dict[str, int] = {}
        try:
            for name in names:
                device[name] = self.cuda.allocate(host[name].nbytes)
                if not self.context.set_tensor_address(name, device[name]):
                    raise TensorRtUnavailable("TensorRT rejected a tensor address")
            self.cuda.copy_to_device(device[input_name], host[input_name])
            if not self.context.execute_async_v3(self.cuda.stream.value):
                raise TensorRtUnavailable("TensorRT execution failed")
            for name in names:
                if name != input_name:
                    self.cuda.copy_from_device(host[name], device[name])
            self.cuda.synchronize()
        finally:
            for pointer in device.values():
                self.cuda.free(pointer)
        return {name: host[name] for name in names if name != input_name}

    def _infer_bindings(self, input_array: np.ndarray) -> dict[str, np.ndarray]:
        """TensorRT 8.2 explicit-batch API used by JetPack 4 on Nano."""
        engine, context, trt = self.engine, self.context, self.trt
        indices = range(engine.num_bindings)
        inputs = [i for i in indices if engine.binding_is_input(i)]
        if len(inputs) != 1 or engine.has_implicit_batch_dimension:
            raise TensorRtUnavailable("model must have one explicit-batch input")
        if any(engine.is_shape_binding(i) for i in indices):
            raise TensorRtUnavailable("shape bindings are unsupported")
        input_index = inputs[0]
        contiguous = np.ascontiguousarray(
            input_array, dtype=trt.nptype(engine.get_binding_dtype(input_index))
        )
        if not context.set_binding_shape(input_index, contiguous.shape):
            raise TensorRtUnavailable("TensorRT rejected the input shape")
        host = {input_index: contiguous}
        for i in indices:
            if i == input_index:
                continue
            shape = tuple(context.get_binding_shape(i))
            if any(dimension <= 0 for dimension in shape):
                raise TensorRtUnavailable("TensorRT output shape is unresolved")
            host[i] = np.empty(shape, dtype=trt.nptype(engine.get_binding_dtype(i)))
        bindings = [0] * engine.num_bindings
        try:
            for i in indices:
                bindings[i] = self.cuda.allocate(host[i].nbytes)
            self.cuda.copy_to_device(bindings[input_index], contiguous)
            if not context.execute_async_v2(
                bindings=bindings, stream_handle=self.cuda.stream.value
            ):
                raise TensorRtUnavailable("TensorRT execution failed")
            for i in indices:
                if i != input_index:
                    self.cuda.copy_from_device(host[i], bindings[i])
        finally:
            # Complete outstanding transfers even when enqueue/copy fails.
            try:
                self.cuda.synchronize()
            finally:
                for pointer in bindings:
                    if pointer:
                        self.cuda.free(pointer)
        return {engine.get_binding_name(i): host[i] for i in indices if i != input_index}


class TensorRtSurfTrackModels:
    def __init__(
        self,
        detector_path: Path,
        action_path: Path,
        engine_dir: Path,
        *,
        score_threshold: float = 0.5,
        ride_exit_seconds: float = 5.0,
    ) -> None:
        self.detector_path = Path(detector_path)
        self.action_path = Path(action_path)
        self.engine_dir = Path(engine_dir)
        self.score_threshold = score_threshold
        self.detector: TensorRtRunner | None = None
        self.action: TensorRtRunner | None = None
        self.tracker = IoUTracker(match_iou=0.25, grace_seconds=ride_exit_seconds)

    def load(self) -> None:
        cuda = _CudaDriver()
        self.detector = TensorRtRunner(
            self.detector_path,
            self.engine_dir / "surfer-fcos.fp16.engine",
            dynamic_batch=False,
            cuda=cuda,
        )
        self.action = TensorRtRunner(
            self.action_path,
            self.engine_dir / "actions-mobilev3.fp16.engine",
            dynamic_batch=True,
            cuda=cuda,
        )
        self.detector.load()
        self.action.load()

    def infer_jpeg(self, jpeg: bytes) -> tuple[TrackObservation, ...]:
        if self.detector is None or self.action is None:
            raise RuntimeError("models are not loaded")
        with Image.open(io.BytesIO(jpeg)) as source:
            image = source.convert("RGB")
        return self.infer_image(image)

    def infer_image(self, image: Image.Image) -> tuple[TrackObservation, ...]:
        """Accept a decoded frame without a lossy JPEG round trip."""
        if self.detector is None or self.action is None:
            raise RuntimeError("models are not loaded")
        detector_input, resized_width, resized_height = prepare_detector_input(image)
        raw = decode_fcos_outputs(
            self.detector.infer(detector_input),
            resized_width=resized_width,
            resized_height=resized_height,
            score_threshold=self.score_threshold,
        )
        detections = self.tracker.update(raw)
        if not detections:
            return ()
        logits = self.action.infer(prepare_action_input(image, raw))["action_logits"]
        probabilities = _sigmoid(np.asarray(logits, dtype=np.float32)).tolist()
        if len(probabilities) != len(detections):
            raise TensorRtUnavailable("action output count does not match detections")
        return tuple(
            TrackObservation(
                detection,
                ActionPrediction(detection.track_id, *probability),
            )
            for detection, probability in zip(detections, probabilities)
        )
