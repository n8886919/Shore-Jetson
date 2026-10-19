from dataclasses import replace
import os
import pty
import select
import termios

import pytest

from shore_tracker.config import AppConfig, ConfigError, ServoConfig, validate_config
from shore_tracker.servo.pulse_controller import encode_position
from shore_tracker.servo.port_guard import require_control_port
from shore_tracker.servo.simple import SimpleServoClient, serial_client


@pytest.mark.parametrize('angle,wire', [
    (0, b'#00P0500!'), (45, b'#00P1000!'), (90, b'#00P1500!'),
    (135, b'#00P2000!'), (180, b'#00P2500!'), (90.045, b'#00P1501!'),
])
def test_gxservo_180_degree_mapping(angle, wire):
    assert encode_position(angle) == wire


@pytest.mark.parametrize('angle', [-1, 180.001, 359, float('nan'), float('inf')])
def test_never_accepts_youmi_359_degree_targets(angle):
    with pytest.raises(ValueError): encode_position(angle)


def test_direct_m0_config_requires_explicit_9600_and_180_degrees():
    servo = replace(ServoConfig(), protocol='m0_pulse', serial_baud=9600,
                    serial_device='/dev/ttyTHS1')
    validate_config(replace(AppConfig(), servo=servo))
    for bad in [replace(servo, serial_baud=115200), replace(servo, angle_max_deg=359)]:
        with pytest.raises(ConfigError): validate_config(replace(AppConfig(), servo=bad))


def test_debug_uart_and_symlink_are_rejected_before_open(tmp_path):
    link = tmp_path / 'control'
    link.symlink_to('/dev/ttyTCU0')
    for path in ['/dev/ttyTCU0', str(link)]:
        with pytest.raises(ValueError, match='debug UART'):
            require_control_port(path, proc=tmp_path, sys=tmp_path)


def test_kernel_console_is_rejected_by_name_or_active_list(tmp_path):
    (tmp_path / 'cmdline').write_text('console=ttyTHS1,115200 root=example')
    with pytest.raises(ValueError, match='kernel console'):
        require_control_port('/dev/ttyTHS1', proc=tmp_path, sys=tmp_path)
    (tmp_path / 'cmdline').write_text('console=ttyTCU0,115200')
    require_control_port('/dev/ttyTHS1', proc=tmp_path, sys=tmp_path)
    active = tmp_path / 'class/tty/console/active'
    active.parent.mkdir(parents=True)
    active.write_text('tty0 ttyTHS1')
    with pytest.raises(ValueError, match='active system console'):
        require_control_port('/dev/ttyTHS1', proc=tmp_path, sys=tmp_path)


def test_real_serial_driver_on_pseudoterminal_never_writes_before_arm(monkeypatch):
    pytest.importorskip('serial')
    master, slave = pty.openpty()
    client = serial_client(device=os.ttyname(slave), baud=9600, protocol='m0_pulse',
                           angle_min_deg=0, angle_max_deg=180, send_hz=20)
    monkeypatch.setattr(client, '_run', lambda: None)
    try:
        client.start()
        assert termios.tcgetattr(slave)[4:6] == [termios.B9600, termios.B9600]
        client.set_target_degrees(90)
        client._send_target()
        assert not select.select([master], [], [], .02)[0]
        client.arm()
        client._send_target()
        assert not select.select([master], [], [], .02)[0]
        client.set_target_degrees(90)
        client._send_target()
        assert os.read(master, 128) == b'#00P1500!'
        client.stop_motion()
        client.arm()
        client._send_target()
        assert not select.select([master], [], [], .02)[0]
    finally:
        client.close()
        os.close(master)
        os.close(slave)


def test_direct_controller_stale_target_stops_without_sending_stop_bytes(monkeypatch):
    writes = []
    class Writer:
        def open(self): pass
        def close(self): pass
        def write(self, payload): writes.append(payload)
    now = [0.0]
    client = SimpleServoClient(Writer(), protocol='m0_pulse', angle_min_deg=0,
                              angle_max_deg=180, clock=lambda: now[0])
    monkeypatch.setattr(client, '_run', lambda: None)
    client.start()
    try:
        client.arm()
        client.set_target_degrees(90)
        now[0] = .249
        client._send_target()
        now[0] = .250
        client._send_target()
        assert writes == [b'#00P1500!']
        assert client.snapshot().last_error == 'target_expired'
        assert not client.snapshot().armed
    finally: client.close()
