from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    pass


MAX_FRAME_STALE_SECONDS = 2.0
PWM_MANUAL_TILT_LIMITS_DEG = (70.0, 110.0)


@dataclass(frozen=True)
class CameraConfig:
    interface: str = "enP8p1s0"
    rtsp_uri_env: str = "SHORE_TRACKER_RTSP_URI"
    codec: str = "h265"
    latency_ms: int = 250
    rotation_degrees: int = 0
    preview_fps: int = 20
    native_display: bool = False
    decoded_inference: bool = False
    inference_frame_fps: int = 4


@dataclass(frozen=True)
class InferenceConfig:
    enabled: bool = True
    backend: str = "tensorrt"
    detector_model: Path = Path(
        "/var/lib/shore-tracker/models/surfer_fcos_raw.onnx"
    )
    action_model: Path = Path(
        "/var/lib/shore-tracker/models/actions_mobilenet_v3_small.onnx"
    )
    engine_dir: Path = Path("/var/lib/shore-tracker/models/engines")
    score_threshold: float = 0.50
    max_fps: float = 0.0
    confirmation_frames: int = 3


@dataclass(frozen=True)
class RecordingConfig:
    root: Path = Path("/var/lib/shore-tracker/recordings")
    segment_seconds: int = 600
    free_warning_gib: int = 120
    free_stop_gib: int = 100
    record_audio: bool = False
    destination: str = "jetson"


@dataclass(frozen=True)
class ServoConfig:
    enabled: bool = True
    transport: str = "serial"
    protocol: str = "m0_pulse"
    serial_device: str = "/dev/serial/by-id/replace-with-usb-ttl-adapter"
    serial_baud: int = 9600
    send_hz: int = 50
    pelco_address: int = 1
    pelco_pan_speed: int = 8
    pelco_tilt_speed: int = 8
    angle_min_deg: float = 0.0
    angle_max_deg: float = 180.0
    search_min_deg: float = 20.0
    search_max_deg: float = 160.0
    home_deg: float = 90.0
    max_slew_deg_s: float = 5.0
    max_acceleration_deg_s2: float = 10.0
    search_speed_deg_s: float = 2.0
    pan_gain_deg: float = 45.0
    pan_deadband: float = 0.20
    pan_direction: int = 0
    pan_channel: int = 0
    tilt_channel: int = 1
    tilt_enabled: bool = False
    tilt_direction: int = 0
    tilt_min_deg: float = 85.0
    tilt_max_deg: float = 95.0
    tilt_home_deg: float = 90.0


@dataclass(frozen=True)
class TrackingConfig:
    chasing_threshold: float = 0.70
    takeoff_threshold: float = 0.65
    surfing_threshold: float = 0.70
    candidate_hold_seconds: float = 0.50
    finished_hold_seconds: float = 2.00
    ride_exit_seconds: float = 5.00
    frame_stale_seconds: float = 1.50


@dataclass(frozen=True)
class ZoomConfig:
    enabled: bool = True
    maximum: float = 2.5
    target_box_height: float = 0.28
    edge_margin: float = 0.18
    fast_target_speed: float = 0.35
    slow_inference_seconds: float = 0.35
    zoom_in_per_second: float = 0.35
    zoom_out_per_second: float = 4.0


@dataclass(frozen=True)
class WebConfig:
    h264_preview: bool = False
    allow_lan: bool = False
    host: str = "127.0.0.1"
    port: int = 80
    preview_stale_seconds: float = 2.0


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path = Path("/var/lib/shore-tracker")
    camera: CameraConfig = CameraConfig()
    inference: InferenceConfig = InferenceConfig()
    recording: RecordingConfig = RecordingConfig()
    servo: ServoConfig = ServoConfig()
    tracking: TrackingConfig = TrackingConfig()
    zoom: ZoomConfig = ZoomConfig()
    web: WebConfig = WebConfig()

    def secret_present(self, variable_name: str) -> bool:
        return bool(os.environ.get(variable_name))


def _table(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], section: str = "") -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        prefix = f"{section}." if section else ""
        raise ConfigError(f"unknown configuration key: {prefix}{unknown[0]}")


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _path(value: Any, field: str) -> Path:
    result = Path(_string(value, field))
    if not result.expanduser().is_absolute():
        raise ConfigError(f"{field} must be an absolute path")
    return result.expanduser()


def _bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{field} must be a boolean")
    return value


def _int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} must be an integer")
    return value


def _float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{field} must be finite")
    return result


def _make(section: Mapping[str, Any], cls: type, fields: dict[str, tuple[CallableLike, Any]], name: str):
    _reject_unknown(section, set(fields), name)
    values = {}
    for key, (converter, default) in fields.items():
        values[key] = converter(section.get(key, default), f"{name}.{key}")
    return cls(**values)


CallableLike = Any


def load_config(path: str | Path) -> AppConfig:
    try:
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read configuration: {exc}") from exc
    _reject_unknown(raw, {"data_dir", "camera", "inference", "recording", "servo", "tracking", "zoom", "web"})

    camera = _make(_table(raw, "camera"), CameraConfig, {
        "interface": (_string, "enP8p1s0"),
        "rtsp_uri_env": (_string, "SHORE_TRACKER_RTSP_URI"),
        "codec": (_string, "h265"),
        "latency_ms": (_int, 250),
        "rotation_degrees": (_int, 0),
        "preview_fps": (_int, 20),
        "native_display": (_bool, False),
        "decoded_inference": (_bool, False),
        "inference_frame_fps": (_int, 4),
    }, "camera")
    inference = _make(_table(raw, "inference"), InferenceConfig, {
        "enabled": (_bool, True),
        "backend": (_string, "tensorrt"),
        "detector_model": (_path, str(InferenceConfig.detector_model)),
        "action_model": (_path, str(InferenceConfig.action_model)),
        "engine_dir": (_path, str(InferenceConfig.engine_dir)),
        "score_threshold": (_float, 0.50),
        "max_fps": (_float, 0.0),
        "confirmation_frames": (_int, 3),
    }, "inference")
    recording = _make(_table(raw, "recording"), RecordingConfig, {
        "destination": (_string, RecordingConfig.destination),
        "root": (_path, str(RecordingConfig.root)),
        "segment_seconds": (_int, 600),
        "free_warning_gib": (_int, 120),
        "free_stop_gib": (_int, 100),
        "record_audio": (_bool, False),
    }, "recording")
    servo_values = dict(_table(raw, "servo"))
    # Preserve omitted-protocol historical configs. Current profiles explicitly
    # select pelco_d; never silently change an installed controller's wire format.
    if "protocol" not in servo_values and servo_values.get("serial_baud") == 115200:
        servo_values["protocol"] = "shore_spotter"
    if servo_values.get("transport") == "api":
        raise ConfigError(
            'This app removed UDP; for RS485 set servo.transport="serial", protocol="pelco_d", '
            'serial_baud=9600 and serial_device to the Moxa adapter; calibrate axis directions'
        )
    # Older Serial configs also contained UDP-only fields; ignore those keys
    # without ever silently changing an API/UDP configuration to UART.
    servo_values.pop("wifi_interface", None)
    servo_values.pop("control_port", None)
    servo = _make(servo_values, ServoConfig, {
        "enabled": (_bool, True), "transport": (_string, "serial"),
        "protocol": (_string, ServoConfig.protocol),
        "serial_device": (_string, ServoConfig.serial_device), "serial_baud": (_int, ServoConfig.serial_baud),
        "send_hz": (_int, ServoConfig.send_hz), "angle_min_deg": (_float, 0.0),
        "pelco_address": (_int, 1), "pelco_pan_speed": (_int, 8), "pelco_tilt_speed": (_int, 8),
        "angle_max_deg": (_float, 180.0), "search_min_deg": (_float, 20.0),
        "search_max_deg": (_float, 160.0), "home_deg": (_float, 90.0),
        "max_slew_deg_s": (_float, ServoConfig.max_slew_deg_s),
        "max_acceleration_deg_s2": (_float, ServoConfig.max_acceleration_deg_s2),
        "search_speed_deg_s": (_float, ServoConfig.search_speed_deg_s),
        "pan_gain_deg": (_float, 45.0), "pan_deadband": (_float, ServoConfig.pan_deadband),
        "pan_direction": (_int, 0),
        "pan_channel": (_int, ServoConfig.pan_channel), "tilt_channel": (_int, ServoConfig.tilt_channel),
        "tilt_enabled": (_bool, False), "tilt_direction": (_int, 0),
        "tilt_min_deg": (_float, 85.0), "tilt_max_deg": (_float, 95.0),
        "tilt_home_deg": (_float, 90.0),
    }, "servo")
    tracking_values = dict(_table(raw, "tracking"))
    # Accept old deployed TOML files, but the retired field has no effect.
    tracking_values.pop("lost_grace_seconds", None)
    tracking = _make(tracking_values, TrackingConfig, {
        "chasing_threshold": (_float, 0.70), "takeoff_threshold": (_float, 0.65),
        "surfing_threshold": (_float, 0.70), "candidate_hold_seconds": (_float, 0.50),
        "finished_hold_seconds": (_float, 2.00),
        "ride_exit_seconds": (_float, 5.00),
        "frame_stale_seconds": (_float, 1.50),
    }, "tracking")
    zoom = _make(_table(raw, "zoom"), ZoomConfig, {
        "enabled": (_bool, True), "maximum": (_float, 2.5),
        "target_box_height": (_float, 0.28), "edge_margin": (_float, 0.18),
        "fast_target_speed": (_float, 0.35),
        "slow_inference_seconds": (_float, 0.35),
        "zoom_in_per_second": (_float, 0.35),
        "zoom_out_per_second": (_float, 4.0),
    }, "zoom")
    web = _make(_table(raw, "web"), WebConfig, {
        "host": (_string, "127.0.0.1"), "port": (_int, WebConfig.port),
        "allow_lan": (_bool, False),
        "h264_preview": (_bool, False),
        "preview_stale_seconds": (_float, 2.0),
    }, "web")
    config = AppConfig(
        data_dir=_path(raw.get("data_dir", "/var/lib/shore-tracker"), "data_dir"),
        camera=camera,
        inference=inference,
        recording=recording,
        servo=servo,
        tracking=tracking,
        zoom=zoom,
        web=web,
    )
    validate_config(config)
    return config


def validate_config(cfg: AppConfig) -> None:
    if type(cfg.camera.decoded_inference) is not bool:
        raise ConfigError("camera.decoded_inference must be a boolean")
    if type(cfg.camera.inference_frame_fps) is not int or not 1 <= cfg.camera.inference_frame_fps <= 20:
        raise ConfigError("camera.inference_frame_fps must be an integer in [1, 20]")
    if cfg.camera.codec not in {"h264", "h265"}:
        raise ConfigError("camera.codec must be h264 or h265")
    if type(cfg.camera.rotation_degrees) is not int or cfg.camera.rotation_degrees not in {0, 180}:
        raise ConfigError("camera.rotation_degrees must be 0 or 180")
    if type(cfg.camera.preview_fps) is not int or not 1 <= cfg.camera.preview_fps <= 60:
        raise ConfigError("camera.preview_fps must be an integer in [1, 60]")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}", cfg.camera.interface) is None:
        raise ConfigError("camera.interface is invalid")
    if cfg.inference.backend != "tensorrt":
        raise ConfigError("inference.backend must be tensorrt")
    for name, value in (
        ("detector_model", cfg.inference.detector_model),
        ("action_model", cfg.inference.action_model),
        ("engine_dir", cfg.inference.engine_dir),
        ("recording.root", cfg.recording.root),
        ("data_dir", cfg.data_dir),
    ):
        if not isinstance(value, Path) or not value.expanduser().is_absolute():
            raise ConfigError(f"{name} must be an absolute path")
    if not 0.0 <= cfg.inference.score_threshold <= 1.0:
        raise ConfigError("inference.score_threshold must be in [0, 1]")
    if not 1 <= cfg.inference.confirmation_frames <= 10:
        raise ConfigError("inference.confirmation_frames must be in [1, 10]")
    if (type(cfg.inference.max_fps) not in {int, float}
            or not math.isfinite(cfg.inference.max_fps)
            or not (cfg.inference.max_fps == 0 or 1 <= cfg.inference.max_fps <= 10)):
        raise ConfigError("inference.max_fps must be 0 (unlimited) or in [1, 10]")
    if cfg.recording.destination not in {"jetson", "camera_sd"}:
        raise ConfigError("recording.destination must be jetson or camera_sd")
    if cfg.recording.segment_seconds != 600 or cfg.recording.record_audio:
        raise ConfigError("recording must be 600-second MKV without audio")
    if cfg.recording.free_stop_gib <= 0 or cfg.recording.free_stop_gib >= cfg.recording.free_warning_gib:
        raise ConfigError("recording free-space thresholds are invalid")
    if cfg.servo.transport != "serial":
        raise ConfigError("servo.transport must be serial (direct controller UART)")
    expected_baud = {"shore_spotter": 115200, "m0_pulse": 9600, "pelco_d": 9600}.get(cfg.servo.protocol)
    if expected_baud is None:
        raise ConfigError("servo.protocol must be pelco_d, shore_spotter or m0_pulse")
    if cfg.servo.serial_baud != expected_baud:
        raise ConfigError(f"servo.serial_baud must be {expected_baud} for {cfg.servo.protocol}")
    if not cfg.servo.serial_device.startswith("/dev/"):
        raise ConfigError("servo.serial_device must be under /dev")
    if not 10 <= cfg.servo.send_hz <= 50:
        raise ConfigError("servo.send_hz must be in [10, 50] for the 250 ms host deadline")
    if cfg.servo.protocol != "pelco_d":
        maximum = 180
        if not 0 <= cfg.servo.angle_min_deg < cfg.servo.angle_max_deg <= maximum:
            raise ConfigError(f"servo angle limits must be within [0, {maximum}]")
        if not cfg.servo.angle_min_deg <= cfg.servo.search_min_deg < cfg.servo.search_max_deg <= cfg.servo.angle_max_deg:
            raise ConfigError("servo search range is invalid")
        if not cfg.servo.search_min_deg <= cfg.servo.home_deg <= cfg.servo.search_max_deg:
            raise ConfigError("servo.home_deg must be inside search range")
        if cfg.servo.max_slew_deg_s <= 0 or not 0 < cfg.servo.search_speed_deg_s <= cfg.servo.max_slew_deg_s:
            raise ConfigError("servo speeds are invalid")
        if not math.isfinite(cfg.servo.max_acceleration_deg_s2) or cfg.servo.max_acceleration_deg_s2 <= 0:
            raise ConfigError("servo acceleration must be finite and positive")
    if not math.isfinite(cfg.servo.pan_gain_deg) or cfg.servo.pan_gain_deg <= 0 or not 0 <= cfg.servo.pan_deadband < 0.5:
        raise ConfigError("P control settings are invalid")
    if type(cfg.servo.pan_direction) is not int or cfg.servo.pan_direction not in {-1, 0, 1}:
        raise ConfigError("servo.pan_direction must be -1, 0, or 1")
    if type(cfg.servo.tilt_direction) is not int or cfg.servo.tilt_direction not in {-1, 0, 1}:
        raise ConfigError("servo.tilt_direction must be -1, 0, or 1")
    if cfg.servo.protocol != "pelco_d":
        if (type(cfg.servo.pan_channel) is not int or type(cfg.servo.tilt_channel) is not int
                or {cfg.servo.pan_channel, cfg.servo.tilt_channel} != {0, 1}):
            raise ConfigError("pan_channel and tilt_channel must be distinct 0/1 channels")
        if not (0 <= cfg.servo.tilt_min_deg < cfg.servo.tilt_max_deg <= 180
                and cfg.servo.tilt_min_deg <= cfg.servo.tilt_home_deg <= cfg.servo.tilt_max_deg):
            raise ConfigError("servo tilt limits/home are invalid")
    for name, maximum in (("pelco_address", 255), ("pelco_pan_speed", 63), ("pelco_tilt_speed", 63)):
        value = getattr(cfg.servo, name)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ConfigError(f"servo.{name} must be an integer in [1, {maximum}]")
    if cfg.servo.tilt_enabled and (cfg.servo.protocol not in {"m0_pulse", "pelco_d"} or cfg.servo.tilt_direction == 0):
        raise ConfigError("tilt requires m0_pulse or pelco_d and a calibrated direction")
    for name, value in (
        ("chasing", cfg.tracking.chasing_threshold),
        ("takeoff", cfg.tracking.takeoff_threshold),
        ("surfing", cfg.tracking.surfing_threshold),
    ):
        if not 0 <= value <= 1:
            raise ConfigError(f"tracking.{name}_threshold must be in [0, 1]")
    if not 0 < cfg.tracking.frame_stale_seconds <= MAX_FRAME_STALE_SECONDS:
        raise ConfigError("tracking.frame_stale_seconds is invalid")
    if cfg.tracking.candidate_hold_seconds < 0 or cfg.tracking.finished_hold_seconds <= 0:
        raise ConfigError("tracking timing is invalid")
    if not math.isfinite(cfg.tracking.ride_exit_seconds) or cfg.tracking.ride_exit_seconds <= 0:
        raise ConfigError("tracking.ride_exit_seconds must be positive and finite")
    if not 1.0 <= cfg.zoom.maximum <= 6.0:
        raise ConfigError("zoom.maximum must be in [1, 6]")
    if not 0 < cfg.zoom.target_box_height <= 1 or not 0 < cfg.zoom.edge_margin < 0.5:
        raise ConfigError("zoom framing settings are invalid")
    if cfg.zoom.fast_target_speed <= 0 or cfg.zoom.slow_inference_seconds <= 0:
        raise ConfigError("zoom speed settings are invalid")
    if cfg.zoom.zoom_in_per_second <= 0 or cfg.zoom.zoom_out_per_second <= cfg.zoom.zoom_in_per_second:
        raise ConfigError("zoom must pull out faster than it pushes in")
    if cfg.web.h264_preview and cfg.camera.preview_fps != 10:
        raise ConfigError("H.265 preview currently requires camera.preview_fps=10")
    if cfg.web.h264_preview and not cfg.camera.native_display:
        raise ConfigError("H.265 preview requires camera.native_display")
    if cfg.web.host not in {"127.0.0.1", "::1", "localhost"} and not (cfg.web.allow_lan and cfg.web.host == "0.0.0.0"):
        raise ConfigError("web.host must remain loopback-only")
    if not 1 <= cfg.web.port <= 65535 or cfg.web.preview_stale_seconds <= 0:
        raise ConfigError("web settings are invalid")
