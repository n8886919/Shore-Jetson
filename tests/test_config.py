from pathlib import Path

import pytest

from shore_tracker.config import AppConfig, ConfigError, load_config, validate_config


def test_default_config_is_simple_local_mkv_and_tensorrt() -> None:
    config = AppConfig()
    validate_config(config)
    assert config.inference.backend == "tensorrt"
    assert config.recording.record_audio is False
    assert config.web.port == 80
    assert config.servo.protocol == 'm0_pulse'  # Omitted protocol retains old configs.
    assert config.servo.serial_baud == 9600
    assert not hasattr(config, "drive")
    assert not hasattr(config.servo, "expected_profile_crc")
    assert not hasattr(config.servo, "host_ack_timeout_ms")


def test_web_port_defaults_to_http_and_preserves_explicit_ports(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('')
    assert load_config(path).web.port == 80
    for port in (80, 8090, 18092):
        path.write_text(f'[web]\nport={port}\n')
        assert load_config(path).web.port == port


@pytest.mark.parametrize('name', [
    'example.toml', 'jetson-disarmed.toml', 'jetson-nano.toml',
    'jetson-m0-uart.toml', 'jetson-staging-live.example.toml',
])
def test_supplied_profiles_use_http_port(name):
    assert load_config(Path('config') / name).web.port == 80


def test_load_rejects_removed_drive_and_servo_session_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[drive]\nenabled=false\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown configuration key"):
        load_config(path)

    path.write_text("[servo]\nhost_ack_timeout_ms=250\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown configuration key"):
        load_config(path)


def test_deployment_config_selects_rs485_but_waits_for_calibration() -> None:
    config = load_config(Path("config/jetson-disarmed.toml"))
    assert config.zoom.maximum == 2.5
    assert config.servo.transport == "serial"
    assert config.servo.serial_baud == 9600
    assert config.servo.protocol == 'pelco_d'
    assert config.servo.enabled is False
    assert config.servo.pan_direction == 0
    assert config.servo.pelco_address == 1
    assert config.servo.pelco_pan_speed == 8


def test_legacy_udp_config_requires_explicit_uart_migration(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[servo]\ntransport="api"\ncontrol_port=42142\n')
    with pytest.raises(ConfigError, match="removed UDP"):
        load_config(path)


def test_legacy_serial_config_keeps_its_device_and_ignores_udp_fields(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[servo]\ntransport="serial"\nserial_device="/dev/serial/by-id/test"\n'
                    'wifi_interface="unused"\ncontrol_port=42142\n')
    config = load_config(path)
    assert config.servo.serial_device == "/dev/serial/by-id/test"
    assert not hasattr(config.servo, "control_port")


@pytest.mark.parametrize("setting, field", [
    ('transport="udp"', "transport"),
    ('serial_baud=115200', "serial_baud"),
    ('send_hz=4', "send_hz"),
    ('send_hz=51', "send_hz"),
    ('angle_min_deg=-1', "angle limits"),
    ('angle_max_deg=181', "angle limits"),
])
def test_uart_config_rejects_incompatible_settings(tmp_path, setting, field):
    path = tmp_path / "config.toml"
    path.write_text(f'[servo]\nprotocol="m0_pulse"\n{setting}\n')
    with pytest.raises(ConfigError, match=field):
        load_config(path)


@pytest.mark.parametrize("rotation", [0, 180])
def test_camera_rotation_loads(tmp_path: Path, rotation: int) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[camera]\nrotation_degrees={rotation}\n")
    assert load_config(path).camera.rotation_degrees == rotation


def test_camera_rotation_rejects_unsupported_orientation(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[camera]\nrotation_degrees=90\n")
    with pytest.raises(ConfigError, match="rotation_degrees"):
        load_config(path)


def test_ride_exit_defaults_to_five_seconds_and_can_be_configured(tmp_path):
    path=tmp_path/'config.toml'
    path.write_text('[tracking]\nfinished_hold_seconds=2\n')
    assert load_config(path).tracking.ride_exit_seconds == 5
    path.write_text('[tracking]\nride_exit_seconds=4\n')
    assert load_config(path).tracking.ride_exit_seconds == 4
    path.write_text('[tracking]\nride_exit_seconds=0\n')
    with pytest.raises(ConfigError,match='ride_exit_seconds'):load_config(path)


def test_preview_fps_is_configurable_independently_of_inference(tmp_path):
    path=tmp_path/'config.toml'
    path.write_text('[camera]\npreview_fps=15\n')
    config=load_config(path)
    assert config.camera.preview_fps == 15
    assert config.inference == AppConfig().inference
    path.write_text('[camera]\npreview_fps=0\n')
    with pytest.raises(ConfigError,match='preview_fps'):load_config(path)


def test_legacy_loss_timeout_cannot_override_shared_grace(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[tracking]\nlost_grace_seconds=0.1\nride_exit_seconds=5\n")
    config = load_config(path)
    assert config.tracking.ride_exit_seconds == 5
    assert not hasattr(config.tracking, "lost_grace_seconds")


def test_inference_rate_defaults_to_unlimited_and_is_independent_of_preview(tmp_path):
    path=tmp_path/'rate.toml';path.write_text('[camera]\npreview_fps=20\n')
    config=load_config(path)
    assert config.inference.max_fps==0
    path.write_text('[inference]\nmax_fps=5\n')
    assert load_config(path).inference.max_fps==5
    for value in ('-1','.5','11','true','nan','inf'):
        path.write_text('[inference]\nmax_fps='+value+'\n')
        with pytest.raises(ConfigError):load_config(path)


def test_tolerance_default_is_twenty_five_percent_total(tmp_path):
    from shore_tracker.status import StatusStore
    path=tmp_path/'empty.toml';path.write_text('')
    assert AppConfig().servo.pan_deadband == .20
    assert load_config(path).servo.pan_deadband == .20
    assert StatusStore().snapshot()['tuning']['pan_deadband'] == .20
