from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from shore_tracker.inference.tensorrt_runtime import (
    ACTION_SIZE,
    DETECTOR_HEIGHT,
    DETECTOR_WIDTH,
    FEATURE_LEVELS,
    TensorRtSurfTrackModels,
    TensorRtRunner,
    decode_fcos_outputs,
    prepare_action_input,
    prepare_detector_input,
)


def output_tensors(*, indices=(0,)) -> dict[str, np.ndarray]:
    count = sum(height * width for height, width, _stride in FEATURE_LEVELS)
    cls = np.full((1, count, 1), -40.0, dtype=np.float32)
    ctr = np.full((1, count, 1), -40.0, dtype=np.float32)
    boxes = np.ones((1, count, 4), dtype=np.float32)
    for index in indices:
        cls[0, index, 0] = 20.0
        ctr[0, index, 0] = 20.0
    return {
        "cls_logits": cls,
        "bbox_regression": boxes,
        "bbox_ctrness": ctr,
    }


def test_detector_preprocess_is_top_left_letterboxed_rgb_float32() -> None:
    image = Image.new("RGB", (1920, 1080), (10, 20, 30))
    tensor, width, height = prepare_detector_input(image)
    assert (width, height) == (896, 504)
    assert tensor.shape == (1, 3, DETECTOR_HEIGHT, DETECTOR_WIDTH)
    assert tensor.dtype == np.float32
    assert tensor[0, :, 0, 0].tolist() == [10.0, 20.0, 30.0]
    assert tensor[0, :, 511, 0].tolist() == pytest.approx(
        [123.675, 116.28, 103.53]
    )


def test_fcos_decode_uses_level_stride_threshold_and_normalizes() -> None:
    detections = decode_fcos_outputs(
        output_tensors(indices=(0,)),
        resized_width=DETECTOR_WIDTH,
        resized_height=DETECTOR_HEIGHT,
        score_threshold=0.5,
    )
    assert len(detections) == 1
    box = detections[0].box
    assert box.x == 0.0
    assert box.y == 0.0
    assert box.width == pytest.approx(8 / DETECTOR_WIDTH)
    assert box.height == pytest.approx(8 / DETECTOR_HEIGHT)
    assert detections[0].confidence > 0.99


def test_fcos_decode_rejects_wrong_output_shape() -> None:
    outputs = output_tensors()
    outputs["cls_logits"] = np.zeros((1, 2, 1), dtype=np.float32)
    with pytest.raises(ValueError, match="output shapes"):
        decode_fcos_outputs(
            outputs,
            resized_width=896,
            resized_height=512,
            score_threshold=0.5,
        )


def test_fcos_default_keeps_top_ten_after_suppressing_duplicates() -> None:
    # Twelve separate people, plus a higher-scoring duplicate of the first.
    indices = tuple(8 * 112 + 8 + 6 * i for i in range(12))
    duplicate = indices[0] + 1
    outputs = output_tensors(indices=(*indices, duplicate))
    outputs["bbox_regression"].fill(3)
    for rank, index in enumerate((*indices, duplicate)):
        outputs["cls_logits"][0, index, 0] = 1 + rank / 10
        outputs["bbox_ctrness"][0, index, 0] = 1 + rank / 10
    detections = decode_fcos_outputs(
        outputs, resized_width=896, resized_height=512, score_threshold=0.5,
    )
    assert len(detections) == 10
    scores = [d.confidence for d in detections]
    expected = [1 / (1 + np.exp(-(1 + rank / 10))) for rank in range(12, 2, -1)]
    assert scores == pytest.approx(expected)


def test_model_limits_action_batch_and_tracks_to_ten_candidates() -> None:
    class CrowdedDetector:
        def infer(self, _tensor):
            return output_tensors(indices=tuple(8 * 112 + 8 + 6 * i for i in range(12)))

    class Actions:
        def infer(self, tensor):
            assert tensor.shape == (10, 3, ACTION_SIZE, ACTION_SIZE)
            return {"action_logits": np.zeros((10, 3), dtype=np.float32)}

    model = TensorRtSurfTrackModels(Path("detector.onnx"), Path("action.onnx"), Path("engines"))
    model.detector = CrowdedDetector()
    model.action = Actions()
    assert len(model.infer_image(Image.new("RGB", (896, 512)))) == 10


def test_action_preprocess_uses_dynamic_batch_raw_rgb() -> None:
    detections = decode_fcos_outputs(
        output_tensors(indices=(1000, 3000)),
        resized_width=896,
        resized_height=512,
        score_threshold=0.5,
    )
    tensor = prepare_action_input(Image.new("RGB", (896, 512), (3, 4, 5)), detections)
    assert tensor.shape == (2, 3, ACTION_SIZE, ACTION_SIZE)
    assert tensor.dtype == np.float32
    assert tensor[0, :, 0, 0].tolist() == [3.0, 4.0, 5.0]


class FakeDetector:
    def infer(self, _input):
        return output_tensors(indices=(1000,))


class FakeAction:
    def infer(self, input_array):
        assert input_array.shape[0] == 1
        return {"action_logits": np.array([[4.0, -4.0, -4.0]], dtype=np.float32)}


def test_model_adapter_connects_detector_tracker_and_actions() -> None:
    model = TensorRtSurfTrackModels(Path("detector.onnx"), Path("action.onnx"), Path("engines"))
    model.detector = FakeDetector()  # type: ignore[assignment]
    model.action = FakeAction()  # type: ignore[assignment]
    image = Image.new("RGB", (896, 512), (3, 4, 5))
    payload = io.BytesIO()
    image.save(payload, format="JPEG")
    observations = model.infer_jpeg(payload.getvalue())
    assert len(observations) == 1
    assert observations[0].action.chasing_wave > 0.98
    assert observations[0].action.takeoff < 0.02


class FakeTensorMode:
    INPUT = "input"
    OUTPUT = "output"


class FakeTrt:
    TensorIOMode = FakeTensorMode

    @staticmethod
    def nptype(_dtype):
        return np.float32


class FakeEngine:
    names = ("images", "cls_logits", "bbox_regression", "bbox_ctrness")
    num_io_tensors = 4

    def get_tensor_name(self, index):
        return self.names[index]

    def get_tensor_mode(self, name):
        return FakeTensorMode.INPUT if name == "images" else FakeTensorMode.OUTPUT

    def get_tensor_dtype(self, _name):
        return "float32"


class FakeContext:
    def __init__(self):
        self.addresses = {}

    def set_input_shape(self, name, shape):
        return name == "images" and shape == (1, 3, 512, 896)

    def get_tensor_shape(self, name):
        return {
            "cls_logits": (1, 9548, 1),
            "bbox_regression": (1, 9548, 4),
            "bbox_ctrness": (1, 9548, 1),
        }[name]

    def set_tensor_address(self, name, pointer):
        self.addresses[name] = pointer
        return True

    def execute_async_v3(self, stream):
        return stream == 7 and len(self.addresses) == 4


class FakeCuda:
    def __init__(self):
        self.next_pointer = 100
        self.freed = []
        self.stream = type("Stream", (), {"value": 7})()

    def make_current(self): pass

    def allocate(self, _size):
        self.next_pointer += 1
        return self.next_pointer

    def free(self, pointer):
        self.freed.append(pointer)

    def copy_to_device(self, _pointer, _array): pass
    def copy_from_device(self, array, _pointer): array.fill(0)
    def synchronize(self): pass


def test_runner_binds_named_tensors_and_frees_every_allocation() -> None:
    cuda = FakeCuda()
    runner = TensorRtRunner(
        Path("model.onnx"), Path("model.engine"), dynamic_batch=False, cuda=cuda  # type: ignore[arg-type]
    )
    runner.trt = FakeTrt()
    runner.engine = FakeEngine()
    runner.context = FakeContext()
    output = runner.infer(np.zeros((1, 3, 512, 896), dtype=np.float32))
    assert set(output) == {"cls_logits", "bbox_regression", "bbox_ctrness"}
    assert output["bbox_regression"].shape == (1, 9548, 4)
    assert len(cuda.freed) == 4


class LegacyEngine:
    # Deliberately put the input after an output: binding order is not a model
    # contract, while output names and runtime batch sizes are.
    num_bindings = 2
    has_implicit_batch_dimension = False

    def binding_is_input(self, i): return i == 1
    def is_shape_binding(self, i): return False
    def get_binding_name(self, i): return ("action_logits", "images")[i]
    def get_binding_dtype(self, i): return "float32"


class LegacyContext:
    def set_binding_shape(self, index, shape):
        self.batch = shape[0]
        return index == 1 and tuple(shape[1:]) == (3, 160, 160)

    def get_binding_shape(self, index): return (self.batch, 3)

    def execute_async_v2(self, *, bindings, stream_handle):
        assert len(bindings) == 2 and all(bindings) and stream_handle == 7
        return True


def legacy_runner():
    cuda = FakeCuda()
    runner = TensorRtRunner(Path("action.onnx"), Path("action.engine"),
                            dynamic_batch=True, cuda=cuda)
    runner.trt = FakeTrt()
    runner.engine = LegacyEngine()
    runner.context = LegacyContext()
    return runner, cuda


def test_legacy_engine_handles_nonzero_input_binding_and_changing_batch():
    runner, cuda = legacy_runner()
    for batch in (1, 4, 2):
        outputs = runner.infer(np.zeros((batch, 3, 160, 160), dtype=np.float32))
        assert set(outputs) == {"action_logits"}
        assert outputs["action_logits"].shape == (batch, 3)
        assert np.isfinite(outputs["action_logits"]).all()
    assert len(cuda.freed) == 6


def test_legacy_failed_enqueue_synchronizes_before_releasing_memory():
    runner, cuda = legacy_runner()
    events = []
    runner.context.execute_async_v2 = lambda **kwargs: False
    cuda.synchronize = lambda: events.append("sync")
    cuda.free = lambda pointer: events.append("free")
    with pytest.raises(RuntimeError, match="execution failed"):
        runner.infer(np.zeros((1, 3, 160, 160), dtype=np.float32))
    assert events == ["sync", "free", "free"]


def test_legacy_rejects_unresolved_outputs_before_allocating():
    runner, cuda = legacy_runner()
    runner.context.get_binding_shape = lambda i: (-1, 3)
    with pytest.raises(RuntimeError, match="unresolved"):
        runner.infer(np.zeros((1, 3, 160, 160), dtype=np.float32))
    assert cuda.next_pointer == 100
