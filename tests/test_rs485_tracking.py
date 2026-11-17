"""RS485 following and fail-safe behavior using fake ports only."""
import threading
import time
from dataclasses import replace

import pytest

from shore_tracker.config import AppConfig, ConfigError, ServoConfig, TrackingConfig, load_config, validate_config
from shore_tracker.control.velocity import VelocityPlanner, VelocityPlannerConfig
from shore_tracker.coordinator import TrackingCoordinator
from shore_tracker.inference import (ActionPrediction, DetectionPrediction, FollowDecision,
    FollowDecisionReason, FollowState, NormalizedBox, TrackObservation)
from shore_tracker.runtime import ShoreTrackerRuntime
from shore_tracker.servo.pelco_client import PelcoServoClient, pelco_serial_client
from shore_tracker.servo.pelco_d import encode_velocity
from shore_tracker.servo.simple_service import ServoService, build_servo_service
from shore_tracker.status import StatusStore


STOP = bytes.fromhex('ff 01 00 00 00 00 01')


def sample(x=.8, y=.8, *, takeoff=0):
    return TrackObservation(DetectionPrediction(1, NormalizedBox(x-.05, y-.05, .1, .1), .95),
                            ActionPrediction(1, .9, takeoff, 0))


def decision(x=.8, y=.8):
    return FollowDecision(FollowState.SURFING, 1, sample(x, y), FollowDecisionReason.TRACKING)


class Writer:
    def __init__(self): self.writes = []; self.closed = False
    def open(self): pass
    def close(self): self.closed = True
    def write(self, payload): self.writes.append(payload)


def client_fixture(monkeypatch, writer=None):
    now = [0.]
    writer = writer or Writer()
    client = PelcoServoClient(writer, clock=lambda: now[0])
    monkeypatch.setattr(client, '_run', lambda: None)
    client.start()
    return client, writer, now


@pytest.mark.parametrize('pan,tilt,hex_frame', [
    (0, 0, 'ff 01 00 00 00 00 01'),
    (8, -3, 'ff 01 00 0a 08 03 16'),
    (-8, 3, 'ff 01 00 14 08 03 20'),
    (0, 4, 'ff 01 00 10 00 04 15'),
    (63, 0, 'ff 01 00 02 3f 00 42'),
])
def test_wire_frames_include_both_axes_and_clear_stopped_bits(pan, tilt, hex_frame):
    assert encode_velocity(1, pan, tilt) == bytes.fromhex(hex_frame)


@pytest.mark.parametrize('pan,tilt', [(64, 0), (0, -64), (True, 0), (0, 1.5)])
def test_encoder_rejects_invalid_levels(pan, tilt):
    with pytest.raises(ValueError): encode_velocity(1, pan, tilt)


def test_image_following_directions_deadband_caps_and_no_home():
    pan = VelocityPlanner(VelocityPlannerConfig(direction=1))
    tilt = VelocityPlanner(VelocityPlannerConfig(direction=-1, max_speed=3, axis='tilt'))
    assert pan.step(decision(.8, .8), armed=True) > 0
    assert tilt.step(decision(.8, .8), armed=True) < 0
    assert pan.step(decision(.2, .2), armed=True) < 0
    assert tilt.step(decision(.2, .2), armed=True) > 0
    for x in (.375, .5, .625): assert pan.step(decision(x), armed=True) == 0
    for x in (.051, .95): assert 1 <= abs(pan.step(decision(x), armed=True)) <= 8
    pan.config = replace(pan.config, pan_gain_deg=120)
    assert pan.step(decision(.95), armed=True) == 8
    lost = FollowDecision(FollowState.SURFING, 1, None, FollowDecisionReason.TEMPORARILY_LOST)
    assert pan.step(lost, armed=True) == 0
    assert pan.step(None, armed=True) == 0
    assert pan.step(decision(), armed=False) == 0
    pan.config = replace(pan.config, direction=0)
    assert pan.step(decision(), armed=True) == 0


def test_transport_boot_arm_stop_rearm_and_old_run_fence(monkeypatch):
    client, writer, now = client_fixture(monkeypatch)
    try:
        assert not client.snapshot().armed and writer.writes == []
        assert not client.set_velocity(4, 2, run_id=1)
        client.arm(run_id=1); client._send_target()
        assert writer.writes == []
        assert client.set_velocity(4, 2, run_id=1)
        client._send_target()
        assert writer.writes[-1] == encode_velocity(1, 4, 2)
        with pytest.raises(RuntimeError, match='already armed'): client.arm(run_id=1)
        client.stop_motion()
        assert writer.writes[-1] == STOP and not client.snapshot().armed
        client.arm(run_id=2); count = len(writer.writes)
        assert not client.set_velocity(8, 8, run_id=1)
        client._send_target(); assert len(writer.writes) == count
        client.set_velocity(-3, 0, run_id=2); client._send_target()
        assert writer.writes[-1] == encode_velocity(1, -3, 0)
    finally: client.close()
    assert writer.closed and writer.writes[-1] == STOP


def test_target_and_tilt_stop_are_immediate_and_never_replay_cache(monkeypatch):
    client, writer, now = client_fixture(monkeypatch)
    try:
        client.arm(run_id=1)
        client.set_velocity(4, 4, run_id=1); client._send_target()
        client.set_velocity(0, 0, run_id=1)
        assert writer.writes[-1] == STOP  # No wait for worker tick.
        client.set_velocity(4, 4, run_id=1); client._send_target()
        client.disable_tilt(); count = len(writer.writes)
        assert writer.writes[-1] == STOP and client.snapshot().armed
        client._send_target(); assert len(writer.writes) == count
        client.set_velocity(4, 0, run_id=1); client._send_target()
        assert writer.writes[-1] == encode_velocity(1, 4, 0)
        with pytest.raises(ValueError): client.set_velocity(9, 0, run_id=1)
    finally: client.close()


@pytest.mark.parametrize('age', [.25, 2., -.01])
def test_expired_or_reversed_clock_sends_stop_and_latches_error(monkeypatch, age):
    client, writer, now = client_fixture(monkeypatch)
    errors = []; client.on_error = errors.append
    try:
        client.arm(run_id=1); client.set_velocity(4, 0, run_id=1); client._send_target()
        now[0] = age; client._send_target()
        assert writer.writes[-1] == STOP
        assert client.snapshot().last_error == 'target_expired'
        assert not client.snapshot().ready and not client.snapshot().armed
        assert errors == ['target_expired']
        with pytest.raises(RuntimeError): client.arm(run_id=2)
    finally: client.close()


def test_partial_serial_write_attempts_stop_and_disarms(monkeypatch):
    ports = []
    class Port:
        def __init__(self, **kwargs): self.kwargs = kwargs; self.writes = []; ports.append(self)
        def write(self, payload):
            self.writes.append(payload)
            return 3 if payload != STOP else len(payload)
        def close(self): pass
    cfg = replace(ServoConfig(), protocol='pelco_d', serial_device='/dev/serial/by-id/test')
    client = pelco_serial_client(cfg, serial_factory=Port)
    monkeypatch.setattr(client, '_run', lambda: None)
    status = StatusStore(); service = ServoService(client, status)
    for name in ('camera', 'inference'): status.update_component(name, 'ready')
    status.add_disarm_listener(lambda _: service.stop_motion())
    service.start()
    try:
        assert ports[0].kwargs['exclusive'] is True
        assert ports[0].kwargs['baudrate'] == 9600
        assert status.request_arm()[0] and service.request_arm()[0]
        service.set_velocity(4, 0, status.inference_run()[0]); client._send_target()
        assert ports[0].writes[0] == encode_velocity(1, 4, 0)
        assert ports[0].writes[-1] == STOP
        assert not status.snapshot()['tracking']['armed']
        assert status.snapshot()['components']['servo']['state'] == 'error'
    finally: service.close()


def test_stop_waits_for_inflight_write_and_is_last_packet():
    entered = threading.Event(); release = threading.Event()
    class BlockingWriter(Writer):
        def write(self, payload):
            if payload != STOP:
                entered.set(); assert release.wait(2)
            super().write(payload)
    writer = BlockingWriter(); client = PelcoServoClient(writer)
    client.writer.open(); client._opened = client._ready = True
    client.arm(run_id=1); client.set_velocity(4, 0, run_id=1)
    worker = threading.Thread(target=client._send_target); worker.start()
    assert entered.wait(1)
    stopper = threading.Thread(target=client.stop_motion); stopper.start()
    release.set(); worker.join(1); stopper.join(1)
    assert not worker.is_alive() and not stopper.is_alive()
    assert writer.writes == [encode_velocity(1, 4, 0), STOP]
    client._send_target(); assert writer.writes[-1] == STOP
    client.close()


def test_real_writer_thread_expires_a_stalled_producer():
    writer = Writer(); client = PelcoServoClient(writer, send_hz=50)
    client.start()
    try:
        client.arm(run_id=1); client.set_velocity(4, 0, run_id=1)
        deadline = time.monotonic() + 1
        while client.snapshot().ready and time.monotonic() < deadline: time.sleep(.01)
        assert not client.snapshot().ready
        assert encode_velocity(1, 4, 0) in writer.writes
        assert writer.writes[-1] == STOP
    finally: assert client.close()


@pytest.mark.parametrize('field,value', [('pelco_address', 0), ('pelco_address', 256),
    ('pelco_pan_speed', 64), ('pelco_tilt_speed', 0), ('pelco_pan_speed', True),
    ('serial_baud', 2400), ('pan_direction', True)])
def test_invalid_pelco_configuration(field, value):
    servo = replace(ServoConfig(), protocol='pelco_d', **{field: value})
    with pytest.raises(ConfigError): validate_config(replace(AppConfig(), servo=servo))


def test_general_profiles_explicitly_select_rs485_without_assuming_calibration():
    for name in ('example', 'jetson-disarmed', 'jetson-nano', 'jetson-staging-live.example'):
        cfg = load_config(f'config/{name}.toml')
        assert cfg.servo.protocol == 'pelco_d' and cfg.servo.serial_baud == 9600
        assert not cfg.servo.enabled and cfg.servo.pan_direction == 0
    with pytest.raises(ValueError, match='not calibrated'):
        build_servo_service(replace(ServoConfig(), protocol='pelco_d'), StatusStore())


def test_runtime_delivers_rs485_follows_stops_and_has_no_angle_targets(tmp_path, monkeypatch):
    from test_runtime import Camera, Dashboard, Inference, config
    monkeypatch.setenv('SHORE_TRACKER_RTSP_URI', 'rtsp://test:secret@192.0.2.8/live')
    monkeypatch.setattr(TrackingCoordinator, 'start', lambda self: None)
    writer = Writer(); client = PelcoServoClient(writer)
    monkeypatch.setattr(client, '_run', lambda: None)
    base = config(tmp_path)
    cfg = replace(base, servo=replace(base.servo, protocol='pelco_d', pan_direction=1,
        tilt_direction=1, tilt_enabled=True), tracking=replace(TrackingConfig(), candidate_hold_seconds=0))
    runtime = ShoreTrackerRuntime(cfg, dashboard_builder=lambda *a, **k: Dashboard(),
        camera_builder=Camera, inference_worker_builder=Inference,
        servo_service_builder=lambda cfg, status: ServoService(client, status),
        camera_route_validator=lambda *a: None, camera_retry_seconds=60)
    runtime.start_services()
    try:
        assert runtime.diagnostics()['servo']['pelco_address'] == 1
        assert 'home_deg' not in runtime.diagnostics()['servo']
        with pytest.raises(ValueError, match='angle'): runtime._servo.set_target_degrees(90)
        assert not runtime.status.snapshot()['tracking']['armed']
        assert runtime.controller.arm()[0]
        assert writer.writes == []
        coordinator = runtime._coordinator
        for now in (0, .1, .2): coordinator.process_frame([sample()], now=now)
        for now in (.3, .4, .5): coordinator.process_frame([sample(takeoff=.9)], now=now)
        coordinator.control_step(now=.5); client._send_target()
        assert writer.writes[-1][3] == 0x12  # Right + down, independently capped.
        snapshot = runtime.status.snapshot()
        assert snapshot['metrics']['servo_angle_deg'] is None
        assert snapshot['metrics']['tilt_angle_deg'] is None
        assert snapshot['servo_command']['pan_speed'] > 0
        coordinator.process_frame([], now=.6); coordinator.control_step(now=.6)
        assert writer.writes[-1] == STOP  # Stop throughout missing-target grace.
        coordinator.process_frame([sample(takeoff=.9)], now=.7); coordinator.control_step(now=.7)
        client._send_target(); assert writer.writes[-1] != STOP
        coordinator.control_step(now=2.3)
        assert writer.writes[-1] == STOP and not client.snapshot().armed
        assert not runtime.status.snapshot()['tracking']['armed']
        runtime.status.update_component('inference', 'ready')
        assert runtime.controller.arm()[0]
        count = len(writer.writes); coordinator.control_step(now=2.4); client._send_target()
        assert len(writer.writes) == count  # Re-ARM needs a fresh frame/decision.
        assert runtime.controller.stop()[0] and writer.writes[-1] == STOP
    finally: runtime.stop_services()


def test_status_rejects_an_old_run_command_after_stop_and_rearm():
    status = StatusStore()
    for name in ('camera', 'inference', 'servo'): status.update_component(name, 'ready')
    status.arm(); old_run = status.inference_run()[0]
    status.disarm('test'); status.arm()
    status.update_servo_command(protocol='pelco_d', pan=0, tilt=0)
    status.update_servo_command(protocol='pelco_d', pan=8, tilt=8, run_id=old_run)
    assert status.snapshot()['servo_command']['pan_speed'] == 0


def test_uncalibrated_profile_can_tune_deadband_without_enabling_motion():
    commands = []
    coordinator = TrackingCoordinator(TrackingConfig(), replace(ServoConfig(), protocol='pelco_d'),
        StatusStore(), lambda _: pytest.fail('Angle output must never be used'),
        command_velocity=lambda *args: commands.append(args))
    coordinator.update_tuning(pan_gain_deg=45, pan_deadband=.2, pan_direction=0, zoom_maximum=1)
    assert coordinator.status.snapshot()['tuning']['pan_direction'] == 0
    assert commands == []
    with pytest.raises(ValueError, match='固定'):
        coordinator.update_tuning(pan_gain_deg=45, pan_deadband=.2, pan_direction=1, zoom_maximum=1)


def test_rs485_does_not_validate_or_apply_legacy_angle_and_channel_fields():
    cfg = replace(AppConfig(), servo=replace(ServoConfig(), protocol='pelco_d',
        angle_min_deg=-90, angle_max_deg=90, home_deg=999, tilt_min_deg=-15,
        tilt_max_deg=30, tilt_home_deg=999, pan_channel=99, max_slew_deg_s=-1))
    validate_config(cfg)
    c = TrackingCoordinator(cfg.tracking, cfg.servo, StatusStore(), lambda _: None,
        command_velocity=lambda *args: None)
    assert c.home_deg is None and c.current_angle_deg is None and c.current_tilt_deg is None


def test_stop_write_failure_latches_and_never_claims_physical_success(monkeypatch):
    class BrokenWriter(Writer):
        def write(self, payload):
            super().write(payload)
            raise OSError('disconnected')
    client, writer, _ = client_fixture(monkeypatch, BrokenWriter())
    status = StatusStore(); service = ServoService(client, status)
    for name in ('camera', 'inference', 'servo'): status.update_component(name, 'ready')
    assert status.request_arm()[0] and service.request_arm()[0]
    with pytest.raises(OSError): service.stop_motion()
    snapshot = status.snapshot()
    assert not snapshot['tracking']['armed']
    assert snapshot['components']['servo']['state'] == 'error'
    assert snapshot['servo_command']['pan_speed'] is None
    assert client.snapshot().last_error == 'transport_write_failed'
    assert not service.close()
    assert writer.closed


@pytest.mark.parametrize('component', ['camera', 'inference', 'servo'])
def test_component_fault_delivers_wire_stop_and_rejects_delayed_target(monkeypatch, component):
    client, writer, _ = client_fixture(monkeypatch)
    status = StatusStore(); service = ServoService(client, status)
    for name in ('camera', 'inference', 'servo'): status.update_component(name, 'ready')
    status.add_disarm_listener(lambda _: service.stop_motion())
    try:
        assert status.request_arm()[0] and service.request_arm()[0]
        run_id = status.inference_run()[0]
        service.set_velocity(4, 4, run_id); client._send_target()
        status.update_component(component, 'error')
        assert not status.snapshot()['tracking']['armed'] and writer.writes[-1] == STOP
        count = len(writer.writes)
        service.set_velocity(4, 4, run_id); client._send_target()
        assert len(writer.writes) == count
    finally: service.close()
