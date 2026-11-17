import numpy as np
import pytest

from tools.benchmark_nano import timing_summary, benchmark_runner, tile_origins, infer_frame


@pytest.mark.parametrize("count", [2, 4, 5, 10])
def test_extra_action_workload_does_not_fabricate_observations(count):
    from PIL import Image
    from tools.benchmark_nano import ActionLoadModels

    class Model:
        def infer_image(self, image): return ()
        def infer(self, tensor):
            assert tensor.shape == (count, 3, 160, 160)
            return {"action_logits": np.zeros((count, 3))}

    model = Model()
    model.action = model
    loaded = ActionLoadModels(model, count)
    assert loaded.infer_image(Image.new("RGB", (3840, 2160))) == ()


def test_raw_frame_preserves_rgb_channel_order_and_handles_row_padding():
    class Model:
        def infer_image(self, image):
            assert image.size == (1, 2)
            assert list(image.getdata()) == [(12, 34, 56), (78, 90, 123)]
            return ("observation",)

    data = bytes([12, 34, 56, 255, 0, 0, 0, 0, 78, 90, 123, 255, 0, 0, 0, 0])
    assert infer_frame(Model(), (data, 1, 2, 8), "rgba") == ("observation",)


def test_native_tiles_cover_4k_without_resizing_or_gaps():
    xs, ys = tile_origins(3840, 896, 128), tile_origins(2160, 512, 64)
    assert len(xs) * len(ys) == 25
    for positions, size, length in ((xs, 896, 3840), (ys, 512, 2160)):
        assert positions[0] == 0 and positions[-1] + size == length
        assert all(b - a <= size for a, b in zip(positions, positions[1:]))
    assert tile_origins(400, 896, 128) == (0,)


def test_native_tiles_limit_merged_candidates_before_action_classification():
    from PIL import Image
    from tools.benchmark_nano import NativeTiledModels
    from shore_tracker.inference.tensorrt_runtime import FEATURE_LEVELS

    class Models:
        score_threshold = .5

        def infer(self, tensor):
            if tensor.shape[-1] == 160:
                assert tensor.shape[0] == 10
                return {"action_logits": np.zeros((10, 3))}
            count = sum(h * w for h, w, _ in FEATURE_LEVELS)
            scores = np.full((1, count, 1), -40, np.float32)
            scores[0, [8 * 112 + 8 + 6 * i for i in range(12)], 0] = 20
            return {"cls_logits": scores, "bbox_ctrness": scores,
                    "bbox_regression": np.ones((1, count, 4), np.float32)}

    models = Models()
    models.detector = models.action = models
    tiled = NativeTiledModels(models)
    assert len(tiled.infer_image(Image.new("RGB", (1792, 512)))) == 10
    assert tiled.tile_count > 1


def test_fps_uses_elapsed_time_including_waiting_for_camera():
    summary = timing_summary([.05, .05, .05, .05], elapsed=1)
    assert summary["fps"] == 4
    assert summary["mean_ms"] == 50
    assert summary["samples"] == 4


@pytest.mark.parametrize("samples", [[], [0.0], [float("nan")], [-1]])
def test_invalid_timings_are_not_reported_as_fps(samples):
    with pytest.raises(ValueError):
        timing_summary(samples)


def test_benchmark_warmups_are_not_counted_and_invalid_outputs_fail():
    class Model:
        calls = 0

        def infer(self, tensor):
            self.calls += 1
            return {"logits": np.zeros((1, 3))}

    model = Model()
    result = benchmark_runner(model, np.zeros((1, 3, 160, 160)), 4)
    assert model.calls == 7
    assert result["samples"] == 4
    model.infer = lambda _: {"logits": np.array([float("nan")])}
    with pytest.raises(RuntimeError, match="non-finite"):
        benchmark_runner(model, np.zeros((1,)), 1)
