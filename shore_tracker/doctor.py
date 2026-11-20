from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
from typing import Any

from shore_tracker.config import AppConfig
from shore_tracker.servo.port_guard import require_control_port


def _writable_parent(path: Path) -> bool:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current.exists() and os.access(current, os.W_OK)


def run_doctor(config: AppConfig) -> dict[str, Any]:
    tensorrt = importlib.util.find_spec("tensorrt") is not None
    control_port = True
    if config.servo.enabled:
        try:
            require_control_port(config.servo.serial_device)
        except (OSError, ValueError):
            control_port = False
    checks = {
        "camera_interface": (Path("/sys/class/net") / config.camera.interface).exists(),
        "gstreamer": shutil.which("gst-launch-1.0") is not None,
        "rtsp_secret": config.secret_present(config.camera.rtsp_uri_env),
        "inference_runtime": tensorrt,
        "detector_model": config.inference.detector_model.is_file(),
        "action_model": config.inference.action_model.is_file(),
        "engine_directory": _writable_parent(config.inference.engine_dir),
        "servo_direction": (not config.servo.enabled) or config.servo.pan_direction != 0,
        "servo_transport": (
            not config.servo.enabled
            or (
                config.servo.transport == "serial"
                and control_port
                and Path(config.servo.serial_device).exists()
                and os.access(config.servo.serial_device, os.R_OK | os.W_OK)
            )
        ),
    }
    if config.recording.destination == "jetson":
        checks["recording_directory"] = _writable_parent(config.recording.root)
    return {
        "ok": all(checks.values()),
        "scope": "poc_startup_checks",
        "checks": {name: {"ok": ok} for name, ok in checks.items()},
    }
