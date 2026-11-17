from pathlib import Path

from shore_tracker.config import load_config


def test_user_paths_expand_before_model_loading_and_storage_monitoring(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config = tmp_path / 'config.toml'
    config.write_text('data_dir="~/data"\n[inference]\ndetector_model="~/detector.onnx"\n'
                      'action_model="~/action.onnx"\nengine_dir="~/engines"\n'
                      '[recording]\nroot="~/recordings"\n')
    c = load_config(config)
    assert c.data_dir == tmp_path / 'data'
    assert c.inference.detector_model == tmp_path / 'detector.onnx'
    assert c.inference.engine_dir == tmp_path / 'engines'
    assert c.recording.root == tmp_path / 'recordings'
