from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path("deploy/provision-camera-env.py").absolute()
SPEC = importlib.util.spec_from_file_location("provision_camera_env", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_add_credentials_encodes_them_without_changing_stream_path() -> None:
    result = MODULE.add_credentials(
        "rtsp://192.0.2.253:554/live/main?x=1",
        "local user",
        "p@ss:/word",
    )
    assert result == (
        "rtsp://local%20user:p%40ss%3A%2Fword@192.0.2.253:554/live/main?x=1"
    )


def test_private_environment_file_is_quoted_and_mode_0600(tmp_path: Path) -> None:
    output = tmp_path / "secrets.env"
    MODULE.write_environment(output, 'rtsp://u:p@192.0.2.253/a"b')
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.read_text() == (
        'SHORE_TRACKER_RTSP_URI="rtsp://u:p@192.0.2.253/a\\"b"\n'
    )
